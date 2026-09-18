"""Settings tab: every control applies live through config.set (no Save button). Sections Listening ·
Voice · Assistant · System (§4.15). Slow work (listing voices, restarting the mic, self-test, saving audio)
runs on worker threads that hand results back through a queue drained by poll() on the Tk thread (G4)."""
from __future__ import annotations

import datetime as dt
import logging
import queue
import re
import sys
import threading
from pathlib import Path

import customtkinter as ctk

from xyrus.ui import BASE, LOG_FILE, cfg_get, cfg_set, data_dir, open_path, sir_text
from xyrus.ui import theme
from xyrus.ui.theme import (ACCENT, ACCENT_HOVER, AMBER, BG, FAINT, GREEN, LINE, LINE_HOVER, MENU, MUTED,
                            ON_ACCENT, RED, SEG, TEXT)

log = logging.getLogger("xyrus.ui.settings")

WAKE_OPTIONS = ["cyrus", "zeros", "virus", "cirrus", "sirius", "serious"]
HOTKEY_NAMES = {"show_window": "Show window", "push_to_talk": "Push to talk", "stop_speaking": "Stop speaking"}
DEFAULT_VOICE = "Default voice"
DEFAULT_MIC = "Windows default"
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
APPEARANCE_LABELS = {"dark": "Dark", "light": "Light", "system": "Follow Windows"}   # config ui.appearance


def _api_short(api: str) -> str:
    return "MME" if api == "MME" else "DirectSound" if "DirectSound" in api else api


class SettingsTab(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.cfg = app.config
        self.w: dict[str, object] = {}          # controls by config key (tests + live refresh)
        self._jobs: dict[str, str] = {}
        self._results: "queue.Queue[tuple]" = queue.Queue()
        self._mics: dict[str, tuple[str | None, str | None]] = {}
        self._tick = 0
        self.f_label = ctk.CTkFont(size=14)
        self.f_sub = ctk.CTkFont(size=12)
        self.f_head = ctk.CTkFont(size=15, weight="bold")

        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", scrollbar_button_color=LINE,
                                             scrollbar_button_hover_color=LINE_HOVER)
        self.scroll.pack(fill="both", expand=True, padx=4, pady=4)
        self._build_appearance()
        self._build_listening()
        self._build_voice()
        self._build_assistant()
        self._build_system()
        self._load_voices_async()

    # ---------------------------------------------------------------- layout --
    def _card(self, title: str):
        ctk.CTkLabel(self.scroll, text=title, font=self.f_head, text_color=ACCENT, anchor="w").pack(
            fill="x", padx=14, pady=(12, 4))
        card = ctk.CTkFrame(self.scroll, fg_color=BG, corner_radius=12)
        card.pack(fill="x", padx=8, pady=(0, 4))
        card.grid_columnconfigure(0, weight=1)
        card.next_row = 0
        return card

    def _row(self, card, label: str, sub: str = ""):
        r = card.next_row
        card.next_row += 1
        left = ctk.CTkFrame(card, fg_color="transparent")
        left.grid(row=r, column=0, sticky="w", padx=16, pady=6)
        ctk.CTkLabel(left, text=label, font=self.f_label, text_color=TEXT, anchor="w").pack(anchor="w")
        sub_lbl = ctk.CTkLabel(left, text=sub, font=self.f_sub, text_color=MUTED, anchor="w", justify="left",
                               wraplength=430)
        if sub:
            sub_lbl.pack(anchor="w")
        right = ctk.CTkFrame(card, fg_color="transparent")
        right.grid(row=r, column=1, sticky="e", padx=16, pady=6)
        return right, sub_lbl

    def _switch(self, card, key: str, label: str, sub: str = "", on_change=None):
        right, sub_lbl = self._row(card, label, sub)
        sw = ctk.CTkSwitch(right, text="", width=46, progress_color=ACCENT)

        def changed():
            val = bool(sw.get())
            if on_change is not None:
                on_change(val)
            else:
                cfg_set(self.cfg, key, val)

        sw.configure(command=changed)
        if cfg_get(self.cfg, key, False):
            sw.select()
        sw.pack(side="right")
        self.w[key] = sw
        return sw, sub_lbl

    def _menu(self, card, key: str, label: str, values: list[str], current: str, sub: str = "",
              on_change=None, width: int = 110, cast=int):
        right, _ = self._row(card, label, sub)
        m = ctk.CTkOptionMenu(right, values=values, width=width, height=28, font=self.f_sub,
                              dropdown_font=self.f_sub, dynamic_resizing=False, **MENU)
        m.set(current)
        m.configure(command=on_change or (lambda v: cfg_set(self.cfg, key, cast(v))))
        m.pack(side="right")
        self.w[key] = m
        return m

    def _slider(self, card, key: str, label: str, lo: float, hi: float, steps: int, fmt, sub: str = "",
                cast=int, on_apply=None):
        right, _ = self._row(card, label, sub)
        val_lbl = ctk.CTkLabel(right, text="", width=56, font=self.f_sub, text_color=MUTED, anchor="e")
        s = ctk.CTkSlider(right, from_=lo, to=hi, number_of_steps=steps, width=180, progress_color=ACCENT,
                          button_color=ACCENT, button_hover_color=ACCENT_HOVER)
        cur = cfg_get(self.cfg, key, lo)
        s.set(cur)
        val_lbl.configure(text=fmt(cur))

        def moved(v):
            v = cast(round(v, 3)) if cast is float else cast(round(v))
            val_lbl.configure(text=fmt(v))
            self._debounce(key, lambda: (cfg_set(self.cfg, key, v), on_apply and on_apply(v)))

        s.configure(command=moved)
        s.pack(side="left")
        val_lbl.pack(side="left", padx=(8, 0))
        self.w[key] = s
        return s

    def _entry(self, parent, key: str, width: int, placeholder: str = "", on_commit=None):
        e = ctk.CTkEntry(parent, width=width, height=28, fg_color=BG, border_color=LINE, text_color=TEXT,
                         placeholder_text=placeholder)
        cur = cfg_get(self.cfg, key, "")
        if cur:
            e.insert(0, str(cur))

        def commit(_=None):
            if on_commit is not None:
                on_commit(e.get().strip())
            elif e.get().strip() != (cfg_get(self.cfg, key, "") or ""):
                cfg_set(self.cfg, key, e.get().strip())

        e.bind("<Return>", commit, add="+")
        e.bind("<FocusOut>", commit, add="+")
        self.w[key] = e
        return e

    def _debounce(self, key: str, fn, ms: int = 400):
        if key in self._jobs:
            self.after_cancel(self._jobs[key])
        self._jobs[key] = self.after(ms, lambda: (self._jobs.pop(key, None), fn()))

    def _bg(self, fn, done=None):
        """Run fn on a worker; done(result, error) later runs on the Tk thread (via poll)."""
        def work():
            try:
                res, err = fn(), None
            except Exception as e:                 # noqa: BLE001 - reported to the user
                res, err = None, e
                log.exception("settings worker failed")
            if done is not None:
                self._results.put((done, res, err))
        threading.Thread(target=work, name="settings-worker", daemon=True).start()

    # ------------------------------------------------------------ Appearance --
    def _build_appearance(self):
        card = self._card("Appearance")
        right, _ = self._row(card, "Theme", "Dark, Light, or follow the Windows app setting — applies at once")
        cur = str(cfg_get(self.cfg, "ui.appearance", "dark")).lower()
        self.appearance_seg = ctk.CTkSegmentedButton(right, values=list(APPEARANCE_LABELS.values()), height=28,
                                                     font=self.f_sub, command=self._pick_appearance, **SEG)
        self.appearance_seg.set(APPEARANCE_LABELS.get(cur, "Dark"))
        self.appearance_seg.pack(side="right")
        self.w["ui.appearance"] = self.appearance_seg

    def _pick_appearance(self, label: str):
        setting = next((k for k, v in APPEARANCE_LABELS.items() if v == label), "dark")
        top = self.winfo_toplevel()
        if hasattr(top, "set_appearance"):
            top.set_appearance(setting)                 # stores ui.appearance + re-renders every tab
        else:
            cfg_set(self.cfg, "ui.appearance", setting)
            theme.apply(setting)

    def on_theme(self):
        """Keep the selector in step when the mode changed elsewhere (voice "theme:light")."""
        cur = str(cfg_get(self.cfg, "ui.appearance", "dark")).lower()
        label = APPEARANCE_LABELS.get(cur, "Dark")
        if self.appearance_seg.get() != label:
            self.appearance_seg.set(label)

    # ------------------------------------------------------------- Listening --
    def _build_listening(self):
        card = self._card("Listening")
        right, _ = self._row(card, "Microphone", "MME / DirectSound devices only (16 kHz)")
        names = [DEFAULT_MIC]
        self._mics = {DEFAULT_MIC: (None, None)}
        try:
            from xyrus.audio import list_mics
            for m in list_mics():
                label = f"{m.name} ({_api_short(m.hostapi)})"
                if label not in self._mics:
                    self._mics[label] = (m.name, m.hostapi)
                    names.append(label)
        except Exception:
            log.exception("list_mics failed")
        mic = cfg_get(self.cfg, "mic", {}) or {}
        cur = next((lab for lab, (n, api) in self._mics.items()
                    if n == mic.get("name") and (api == mic.get("hostapi") or n is None)), DEFAULT_MIC)
        self.mic_menu = ctk.CTkOptionMenu(right, values=names, width=300, height=28, font=self.f_sub,
                                          dropdown_font=self.f_sub, dynamic_resizing=False, command=self._pick_mic,
                                          **MENU)
        self.mic_menu.set(cur)
        self.mic_menu.pack(side="right")
        self.w["mic"] = self.mic_menu

        right, self.mic_sub = self._row(card, "Mic level", "the bar should move when you speak")
        ctk.CTkButton(right, text="Test mic", width=80, height=28, fg_color=LINE, hover_color=LINE_HOVER,
                      text_color=TEXT, command=self.test_mic).pack(side="right", padx=(10, 0))
        self.level = ctk.CTkProgressBar(right, width=180, height=10, progress_color=GREEN)
        self.level.set(0)
        self.level.pack(side="right")

        right, self.wake_sub = self._row(card, "Wake words", "what the model hears when you say “Xyrus”")
        current = set(cfg_get(self.cfg, "wake_words", []) or [])
        self.wake_boxes = {}
        for w in WAKE_OPTIONS:
            cb = ctk.CTkCheckBox(right, text=w, width=70, checkbox_width=18, checkbox_height=18, font=self.f_sub,
                                 fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._wake_changed)
            if w in current:
                cb.select()
            cb.pack(side="left", padx=(0, 4))
            self.wake_boxes[w] = cb
        self.w["wake_words"] = self.wake_boxes

        self._slider(card, "command_window_s", "Command window", 0, 120, 24,
                     lambda v: "no limit" if int(v) <= 0 else f"{int(v)} s",
                     "how long Xyrus waits for the command after “Waiting for your command” (0 = no limit)")
        self._switch(card, "follow_up", "Follow-up questions", "answer “and after that?” without the wake word")
        self._slider(card, "follow_up_s", "Follow-up window", 3, 20, 17, lambda v: f"{int(v)} s")
        self._switch(card, "chime_on_wake", "Chime on wake word", "a short tone the moment the name is heard")
        self._switch(card, "clap.enabled", "Clap to switch the display",
                     "three claps turn the display off, three more turn it back on")
        self._slider(card, "clap.claps", "Claps needed", 2, 4, 2, lambda v: f"{int(v)} claps",
                     "more claps = fewer accidents (a knock or setting the mic down is never a clap)")
        self._slider(card, "clap.min_peak", "Clap loudness", 0.05, 0.5, 45, lambda v: f"{float(v):.2f}",
                     "raise it if taps or knocks switch the display; lower it if claps are missed", cast=float)
        self._slider(card, "min_speech_peak", "Mic sensitivity", 0.005, 0.08, 75, lambda v: f"{float(v):.3f}",
                     "minimum loudness of speech; raise it if room noise gets through", cast=float)
        right, self.audio_sub = self._row(card, "Save last 30 seconds of audio", "for troubleshooting recognition")
        ctk.CTkButton(right, text="Save audio", width=96, height=28, fg_color=LINE, hover_color=LINE_HOVER,
                      text_color=TEXT, command=self.save_audio).pack(side="right")

    def _pick_mic(self, label: str):
        name, api = self._mics.get(label, (None, None))
        cfg_set(self.cfg, "mic", {"name": name, "hostapi": api or "MME"})
        self._restart_mic("Switched microphone.")

    def _restart_mic(self, text: str):
        cap = getattr(self.app, "capture", None)
        if cap is None:
            return
        try:
            paused = bool(self.app.engine.snapshot().paused)
        except Exception:
            paused = False
        if paused:
            self.mic_sub.configure(text="Paused — the new microphone opens when you resume.", text_color=AMBER)
            return

        def restart():
            cap.stop()
            cap.start()
            return cap.status()

        self.mic_sub.configure(text="Reopening the microphone…", text_color=MUTED)
        self._bg(restart, lambda st, err: self.mic_sub.configure(
            text=f"{text} Speak — the bar should move." if err is None else f"Microphone error: {err}",
            text_color=GREEN if err is None else RED))

    def test_mic(self):
        self._restart_mic("Microphone reopened.")

    def _wake_changed(self):
        words = [w for w, cb in self.wake_boxes.items() if cb.get()]
        if not words:
            self.wake_boxes["cyrus"].select()
            words = ["cyrus"]
            self.wake_sub.configure(text="Keep at least one wake word.", text_color=AMBER)
        else:
            self.wake_sub.configure(text="what the model hears when you say “Xyrus”", text_color=MUTED)
        cfg_set(self.cfg, "wake_words", words)

    def save_audio(self):
        rec = getattr(self.app, "recognizer", None)
        if rec is None:
            self.audio_sub.configure(text="The recognizer isn't running.", text_color=RED)
            return
        path = data_dir() / "recordings" / f"last-30s-{dt.datetime.now():%Y%m%d-%H%M%S}.wav"

        def work():
            path.parent.mkdir(parents=True, exist_ok=True)
            rec.save_last_seconds(path, 30)
            return path

        def done(p, err):
            if err is None:
                self.audio_sub.configure(text=f"Saved {p.name} in {p.parent}", text_color=GREEN)
            else:
                self.audio_sub.configure(text=f"Could not save audio: {err}", text_color=RED)

        self._bg(work, done)

    # ------------------------------------------------------------------ Voice --
    def _build_voice(self):
        card = self._card("Voice")
        cur = cfg_get(self.cfg, "tts.voice", None) or DEFAULT_VOICE
        self.voice_menu = self._menu(card, "tts.voice", "Voice", [cur], cur, "Windows (SAPI) voices on this PC",
                                     on_change=self._pick_voice, width=260)
        self._slider(card, "tts.rate", "Speaking rate", -10, 10, 20, lambda v: f"{int(v):+d}" if v else "0",
                     on_apply=self._apply_rate)
        self._switch(card, "voice_replies", "Voice replies", "speak answers out loud (alerts and timers always speak)")
        right, _ = self._row(card, "Test voice", "hear the current voice and rate")
        ctk.CTkButton(right, text="Test voice", width=96, height=28, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      text_color=ON_ACCENT, command=self.test_voice).pack(side="right")

    def _load_voices_async(self):
        sp = getattr(self.app, "speaker", None)
        if sp is None:
            return

        def done(voices, err):
            if err is None and voices:
                self.voice_menu.configure(values=[DEFAULT_VOICE] + list(voices))

        self._bg(lambda: list(sp.voices()), done)      # SapiSpeaker.voices() may wait for the SAPI thread

    def _pick_voice(self, label: str):
        name = None if label == DEFAULT_VOICE else label
        cfg_set(self.cfg, "tts.voice", name)
        try:
            self.app.speaker.set_voice(name)
        except Exception:
            log.exception("set_voice failed")

    def _apply_rate(self, rate):
        try:
            self.app.speaker.set_rate(int(rate))
        except Exception:
            log.exception("set_rate failed")

    def test_voice(self):
        try:
            from xyrus import replies
            template = getattr(replies, "TEST_VOICE", "Hello{sir}. This is how I sound.")
        except ImportError:
            template = "Hello{sir}. This is how I sound."
        try:
            self.app.speaker.say(sir_text(self.cfg, template), force=True)
        except Exception:
            log.exception("test voice failed")

    # -------------------------------------------------------------- Assistant --
    def _build_assistant(self):
        card = self._card("Assistant")
        right, _ = self._row(card, "Your name", "used only in greetings")
        self._entry(right, "user_name", 180, "e.g. Refath").pack(side="right")
        right, _ = self._row(card, "Address you as", "the honorific at the end of replies; blank for none")
        self._entry(right, "address_as", 180, "sir").pack(side="right")

        right, _ = self._row(card, "Startup greeting", "when Xyrus starts with Windows")
        seg = ctk.CTkSegmentedButton(right, values=["Speak", "Toast", "Off"], height=28, font=self.f_sub,
                                     command=lambda v: cfg_set(self.cfg, "startup_greeting", v.lower()), **SEG)
        seg.set(str(cfg_get(self.cfg, "startup_greeting", "speak")).capitalize())
        seg.pack(side="right")
        self.w["startup_greeting"] = seg

        self._switch(card, "briefing_on_startup", "Briefing on startup", "today's events and to-dos after the greeting")
        self._menu(card, "calendar.default_reminder_min", "Reminder lead time", ["0", "5", "10", "15", "30", "60"],
                   str(cfg_get(self.cfg, "calendar.default_reminder_min", 10)),
                   "minutes before an event added by voice")
        self._menu(card, "calendar.snooze_minutes", "Snooze", ["5", "10", "15", "30"],
                   str(cfg_get(self.cfg, "calendar.snooze_minutes", 10)), "minutes, when you say “snooze”")

        right, self.quiet_sub = self._row(card, "Quiet hours", "lead reminders are skipped; due alerts still speak")
        qh = cfg_get(self.cfg, "quiet_hours", None) or {}
        self.quiet_sw = ctk.CTkSwitch(right, text="", width=46, progress_color=ACCENT, command=self._quiet_changed)
        self.quiet_from = ctk.CTkEntry(right, width=64, height=28, fg_color=BG, border_color=LINE, text_color=TEXT,
                                       justify="center")
        self.quiet_to = ctk.CTkEntry(right, width=64, height=28, fg_color=BG, border_color=LINE, text_color=TEXT,
                                     justify="center")
        self.quiet_from.insert(0, qh.get("from", "23:00"))
        self.quiet_to.insert(0, qh.get("to", "07:00"))
        if qh:
            self.quiet_sw.select()
        self.quiet_from.pack(side="left")
        ctk.CTkLabel(right, text="to", text_color=MUTED, width=24).pack(side="left")
        self.quiet_to.pack(side="left")
        self.quiet_sw.pack(side="left", padx=(12, 0))
        for e in (self.quiet_from, self.quiet_to):
            e.bind("<Return>", lambda _e: self._quiet_changed(), add="+")
            e.bind("<FocusOut>", lambda _e: self._quiet_changed(), add="+")
        self.w["quiet_hours"] = self.quiet_sw

        right, self.brief_sub = self._row(card, "Morning brief", "once a day if the PC is on and there's something planned")
        self.brief_time = self._entry(right, "calendar.morning_brief_time", 64, "08:30",
                                      on_commit=self._brief_time_changed)
        self.brief_time.configure(justify="center")
        self.brief_time.pack(side="left")
        bsw = ctk.CTkSwitch(right, text="", width=46, progress_color=ACCENT,
                            command=lambda: cfg_set(self.cfg, "calendar.morning_brief", bool(bsw.get())))
        if cfg_get(self.cfg, "calendar.morning_brief", True):
            bsw.select()
        bsw.pack(side="left", padx=(12, 0))
        self.w["calendar.morning_brief"] = bsw

        self._switch(card, "confirm_shutdown", "Confirm before shutdown", "ask “yes or no” before shutdown / restart")
        self._menu(card, "shutdown_delay_s", "Shutdown delay", ["5", "10", "30", "60"],
                   str(cfg_get(self.cfg, "shutdown_delay_s", 10)), "seconds before the PC turns off")
        self._switch(card, "youtube_lookup", "YouTube lookup", "“play <song>” finds the video (needs internet)")

        right, _ = self._row(card, "Weather", "opt-in; uses wttr.in")
        city = self._entry(right, "weather.city", 160, "city")
        city.pack(side="left")
        wsw = ctk.CTkSwitch(right, text="", width=46, progress_color=ACCENT,
                            command=lambda: cfg_set(self.cfg, "weather.enabled", bool(wsw.get())))
        if cfg_get(self.cfg, "weather.enabled", False):
            wsw.select()
        wsw.pack(side="left", padx=(12, 0))
        self.w["weather.enabled"] = wsw

    def _quiet_changed(self):
        if not self.quiet_sw.get():
            cfg_set(self.cfg, "quiet_hours", None)
            self.quiet_sub.configure(text="lead reminders are skipped; due alerts still speak", text_color=MUTED)
            return
        f, t = self.quiet_from.get().strip(), self.quiet_to.get().strip()
        if not (HHMM.match(f) and HHMM.match(t)):
            self.quiet_sub.configure(text="Use HH:MM, e.g. 23:00 to 07:00.", text_color=RED)
            return
        norm = lambda s: "%02d:%s" % (int(s.split(":")[0]), s.split(":")[1])
        cfg_set(self.cfg, "quiet_hours", {"from": norm(f), "to": norm(t)})
        self.quiet_sub.configure(text=f"Quiet from {norm(f)} to {norm(t)}.", text_color=MUTED)

    def _brief_time_changed(self, value: str):
        if not HHMM.match(value):
            self.brief_sub.configure(text="Use HH:MM, e.g. 08:30.", text_color=RED)
            return
        value = "%02d:%s" % (int(value.split(":")[0]), value.split(":")[1])
        cfg_set(self.cfg, "calendar.morning_brief_time", value)
        self.brief_sub.configure(text=f"Every day at {value} if there's something planned.", text_color=MUTED)

    # ----------------------------------------------------------------- System --
    def _build_system(self):
        card = self._card("System")
        self.autostart_sw, self.autostart_sub = self._switch(
            card, "autostart", "Start with Windows", "runs hidden in the tray after every sign-in",
            on_change=self._toggle_autostart)
        self._refresh_autostart()

        self.hotkey_labels = {}
        hk_cfg = cfg_get(self.cfg, "hotkeys", {}) or {}
        for key, label in HOTKEY_NAMES.items():
            right, _ = self._row(card, label, "global hotkey")
            combo = ctk.CTkLabel(right, text=hk_cfg.get(key, "—"), font=ctk.CTkFont(family="Consolas", size=13),
                                 text_color=TEXT)
            combo.pack(side="left", padx=(0, 12))
            st = ctk.CTkLabel(right, text="", font=self.f_sub, width=200, anchor="e")
            st.pack(side="left")
            self.hotkey_labels[key] = (combo, st)
        self.refresh_hotkeys()

        right, self.selftest_sub = self._row(card, "Maintenance", "")
        for text, cmd in (("Open log", lambda: self._open(LOG_FILE)), ("Open folder", lambda: self._open(BASE)),
                          ("Run self-test", self.run_selftest)):
            ctk.CTkButton(right, text=text, width=100, height=28, fg_color=LINE, hover_color=LINE_HOVER,
                          text_color=TEXT, command=cmd).pack(side="left", padx=(0, 6))
        try:
            import xyrus
            version = getattr(xyrus, "version", None) or getattr(xyrus, "__version__", "2.0.0")
        except Exception:
            version = "2.0.0"
        ctk.CTkLabel(self.scroll, text=f"Xyrus {version} · closing the window keeps Xyrus in the tray; "
                                       "quit from the tray icon.", text_color=FAINT,
                     font=self.f_sub).pack(anchor="w", padx=16, pady=(10, 14))

    def _toggle_autostart(self, on: bool):
        try:
            from xyrus import autostart
            autostart.set_enabled(on)
        except Exception as e:
            log.exception("autostart toggle failed")
            self.autostart_sub.configure(text=f"Could not change it: {e}", text_color=RED)
        self._refresh_autostart()

    def _refresh_autostart(self):
        try:
            from xyrus import autostart
            on = autostart.enabled()
            ok = autostart.target_ok() if on else True
        except Exception:
            return
        sw = self.autostart_sw
        (sw.select if on else sw.deselect)()
        if on and not ok:
            self.autostart_sub.configure(text="The shortcut starts a different copy — switch off and on to fix it.",
                                         text_color=AMBER)
        else:
            self.autostart_sub.configure(text="runs hidden in the tray after every sign-in", text_color=MUTED)

    def refresh_hotkeys(self):
        hk = getattr(self.app, "hotkeys", None)
        try:
            status = hk.status() if hk is not None else {}
        except Exception:
            status = {}
        for key, (_combo, st) in self.hotkey_labels.items():
            if key not in status:
                st.configure(text="not active", text_color=FAINT)
            elif status[key]:
                st.configure(text="✓ active", text_color=GREEN)
            else:
                st.configure(text="✕ in use by another program", text_color=RED)

    def _open(self, path: Path):
        try:
            open_path(str(path))
        except Exception as e:
            self.selftest_sub.configure(text=f"Could not open {path}: {e}", text_color=RED)

    def run_selftest(self):
        self.selftest_sub.configure(text="Running the self-test…", text_color=MUTED)
        py = BASE / "venv" / "Scripts" / "python.exe"
        exe = str(py if py.exists() else Path(sys.executable).with_name("python.exe"))

        def work():
            from xyrus import winutil
            return winutil.run([exe, str(BASE / "arc.py"), "--selftest"], timeout=600, capture=True).returncode

        def done(code, err):
            if err is None and code == 0:
                self.selftest_sub.configure(text="Self-test passed.", text_color=GREEN)
            else:
                why = f"exit {code}" if err is None else str(err)
                self.selftest_sub.configure(text=f"Self-test failed ({why}) — see arc.log.", text_color=RED)

        self._bg(work, done)

    # ------------------------------------------------------------------- poll --
    def poll(self, level: float | None = None):
        """From the window poll (150 ms): worker results always; live widgets only while visible."""
        while True:
            try:
                done, res, err = self._results.get_nowait()
            except queue.Empty:
                break
            try:
                done(res, err)
            except Exception:
                log.exception("settings result handler failed")
        if not self.winfo_ismapped():
            return
        if level is not None:
            self.level.set(min(1.0, level * 2.5))
        self._tick += 1
        if self._tick % 14 == 0:                        # ~2 s
            self.refresh_hotkeys()
