"""XyrusWindow: header, dialogue banner, six tabs, the 150 ms poll. Tk main thread only (G4).

The poll drains app.ui_queue ("show", "show:<Tab>", "show:Calendar:<iso>[:<iso>]", v1's
"calendar:<iso>[:<iso>]", "hide", "quit", "theme:dark|light|system"), checks the second-instance show
event, reads engine.snapshot() + capture.level(), and updates the header / banner / tabs / tray only when
values changed. A UI bug is logged and never stops the polling.

Appearance: config ui.appearance ("dark" default | "light" | "system"). CTk widgets follow the mode by
themselves (theme colours are (light, dark) pairs); tabs with plain tk widgets or canvases get on_theme().
"system" re-reads Windows' AppsUseLightTheme every few seconds while running.
"""
from __future__ import annotations

import logging
import queue
import time
import tkinter as tk

import customtkinter as ctk

from xyrus.ui import ICON_FILE, cfg_get, cfg_set
from xyrus.ui import theme
from xyrus.ui.home_tab import timer_title
from xyrus.ui.theme import (ACCENT, ACCENT_HOVER, ACCENT_SOFT, AMBER, AMBER_BG, BG, CARD, FAINT, GREEN, LINE,
                            MUTED, ON_ACCENT, RED, SEG_SEL, SEG_SEL_HOVER, SEG_TEXT, TAB_UNSEL, TAB_UNSEL_HOVER,
                            TEXT, TITLE)

log = logging.getLogger("xyrus.ui.window")

TABS = ("Home", "Calendar", "Commands", "Apps", "Routines", "Settings")
POLL_MS = 150
SYSTEM_THEME_POLL_S = 3.0
DEFAULT_GEOMETRY = "1000x700"
ACTIVE_MODES = ("arming", "armed", "follow_up", "dialogue")


def status_text(snap) -> tuple[str, object]:
    """Header label + dot colour for an EngineSnapshot (§4.15)."""
    st = (snap.status or "").strip()
    low = st.lower()
    d = snap.dialogue
    if snap.paused:
        return "Paused · mic off", MUTED
    if "error" in low or "no microphone" in low:
        return st[:1].upper() + st[1:], RED
    if low == "thinking":                      # Whisper is decoding the command (engine capture state)
        return "Thinking…", ACCENT_SOFT
    if low == "hearing":                       # the command is being recorded
        return "Listening…", ACCENT
    if d is not None and d.kind == "confirm" and not d.parked:
        # SPEC-AMBIGUITY: "Confirm shutdown?" - the confirm dialogue's "action" slot names the action when
        # the command provides one; otherwise the header shows the question itself.
        action = (d.slots or {}).get("action")
        return (f"Confirm {action}?" if action else f"Confirm: {d.question}"), AMBER
    if snap.speaking:
        return "Speaking", ACCENT_SOFT
    if snap.mode in ("arming", "armed"):
        left = snap.window_left_s if snap.mode == "armed" else 0
        return ("Listening for your command" + (f" · {left} s" if left else "")), ACCENT
    if snap.mode == "follow_up":
        left = snap.window_left_s
        return ("Listening for a follow-up" + (f" · {left} s" if left else "")), ACCENT
    if d is not None:
        return ("Waiting for your answer" + (f" · {d.seconds_left} s" if d.seconds_left else "")), ACCENT
    if low == "starting":
        return "Starting…", AMBER
    return "Listening", GREEN


def side_text(snap) -> str:
    """Timer label (soonest timer) or the next event."""
    if snap.timers:
        ringing = [t for t in snap.timers if t.ringing]
        if ringing:
            return f"{timer_title(ringing[0].name)} timer · ringing"
        t = min(snap.timers, key=lambda t: t.remaining_s)
        m, s = divmod(max(0, t.remaining_s), 60)
        h, m = divmod(m, 60)
        return f"timer {h}:{m:02d}:{s:02d}" if h else f"timer {m:02d}:{s:02d}"
    if snap.next_event:
        return f"Next: {snap.next_event}"
    return ""


def tray_state(snap) -> str:
    low = (snap.status or "").lower()
    if snap.paused:
        return "paused"
    if "error" in low or "no microphone" in low:
        return "error"
    if snap.mode in ACTIVE_MODES or snap.dialogue is not None:
        return "active"
    return "listening"


def tray_tooltip(snap) -> str:
    if snap.paused:
        return "Xyrus — paused"
    if snap.next_event:
        return f"Xyrus — next: {snap.next_event}"
    return "Xyrus — listening"


def _appearance_setting(value) -> str:
    v = str(value or "dark").lower()
    return v if v in theme.APPEARANCES else "dark"


class XyrusWindow(ctk.CTk):
    def __init__(self, app, start_hidden: bool = False):
        self._appearance = _appearance_setting(cfg_get(app.config, "ui.appearance", "dark"))
        theme.apply(self._appearance)
        theme.reset_fonts()
        super().__init__(fg_color=BG)
        self.app = app
        self._closing = False
        self._snap = None
        self._header_key = None
        self._banner_key = None
        self._tray_key = None
        self._level = -1.0
        self._poll_job = None
        self._effective = theme.mode()
        self._next_system_check = time.monotonic() + SYSTEM_THEME_POLL_S

        self.title("Xyrus")
        try:
            self.geometry(cfg_get(app.config, "window.geometry", DEFAULT_GEOMETRY))
        except tk.TclError:
            self.geometry(DEFAULT_GEOMETRY)
        self.minsize(760, 520)
        try:
            if ICON_FILE.exists():
                self.iconbitmap(str(ICON_FILE))
        except Exception:
            pass
        self.protocol("WM_DELETE_WINDOW", self.hide)

        self._build_header()
        self._build_banner()
        self._build_tabs()
        last = cfg_get(app.config, "window.last_tab", "Home")
        self._select_tab(last if last in TABS else "Home", save=False)
        if start_hidden:
            self.withdraw()
        self._poll_job = self.after(POLL_MS, self._poll)

    # ------------------------------------------------------------ layout --- #
    def _build_header(self):
        head = self.header = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        head.pack(fill="x", padx=16, pady=(16, 8))
        head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(head, text="Xyrus", font=ctk.CTkFont(size=30, weight="bold"),
                     text_color=TITLE).grid(row=0, column=0, padx=(20, 12), pady=(12, 0), sticky="w")
        ctk.CTkLabel(head, text="offline voice assistant", font=ctk.CTkFont(size=13),
                     text_color=MUTED).grid(row=1, column=0, padx=(22, 12), pady=(0, 12), sticky="w")

        right = ctk.CTkFrame(head, fg_color="transparent")
        right.grid(row=0, column=1, rowspan=2, padx=20, sticky="e")
        stat = ctk.CTkFrame(right, fg_color="transparent")          # dot + label stay together
        stat.grid(row=0, column=0, sticky="w")
        self.status_dot = ctk.CTkLabel(stat, text="●", text_color=GREEN, font=ctk.CTkFont(size=18), width=16)
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_lbl = ctk.CTkLabel(stat, text="Listening", font=ctk.CTkFont(size=15, weight="bold"),
                                       text_color=TEXT, anchor="w")
        self.status_lbl.pack(side="left")
        self.side_lbl = ctk.CTkLabel(right, text="", text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w")
        self.side_lbl.grid(row=1, column=0, sticky="w", padx=(22, 0))
        self.mic_lbl = ctk.CTkLabel(right, text="mic", text_color=MUTED, font=ctk.CTkFont(size=11))
        self.mic_lbl.grid(row=0, column=1, padx=(24, 6))
        self.level = ctk.CTkProgressBar(right, width=120, height=10, progress_color=ACCENT, fg_color=LINE)
        self.level.set(0)
        self.level.grid(row=0, column=2)
        self.stop_btn = ctk.CTkButton(right, text="Stop", width=70, fg_color=LINE, hover_color=RED,
                                      text_color=TEXT, text_color_disabled=FAINT, state="disabled",
                                      command=self._stop_speaking)
        self.stop_btn.grid(row=0, column=3, padx=(20, 0))
        self.pause_btn = ctk.CTkButton(right, text="Pause", width=90, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                       text_color=ON_ACCENT, command=self._toggle_pause)
        self.pause_btn.grid(row=0, column=4, padx=(8, 0))
        self._compact = False
        self.bind("<Configure>", self._on_resize, add="+")

    def _on_resize(self, event):
        """Narrow window: drop the mic meter so the buttons never clip; keep the banner text wrapped."""
        if event.widget is not self:
            return
        scale = ctk.ScalingTracker.get_window_scaling(self)
        compact = event.width < 900 * scale
        if compact != self._compact:
            self._compact = compact
            for w in (self.mic_lbl, self.level):
                if compact:
                    w.grid_remove()
                else:
                    w.grid()
        wrap = max(300, int(event.width / scale) - 120)
        if getattr(self, "_wrap", None) != wrap:
            self._wrap = wrap
            self.banner_q.configure(wraplength=wrap)
            self.banner_hint.configure(wraplength=wrap)

    def _build_banner(self):
        b = self.banner = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14, border_width=1, border_color=ACCENT)
        b.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(b, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 0))
        self.banner_caption = ctk.CTkLabel(top, text="Xyrus is asking", text_color=ACCENT,
                                           font=ctk.CTkFont(size=11, weight="bold"))
        self.banner_caption.pack(side="left")
        self.banner_count = ctk.CTkLabel(top, text="", text_color=MUTED, font=ctk.CTkFont(size=12))
        self.banner_count.pack(side="right")
        self.banner_q = ctk.CTkLabel(b, text="", text_color=TITLE, font=ctk.CTkFont(size=16, weight="bold"),
                                     anchor="w", justify="left", wraplength=800)
        self.banner_q.grid(row=1, column=0, sticky="ew", padx=16)
        self.banner_hint = ctk.CTkLabel(b, text="", text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w",
                                        justify="left", wraplength=800)
        self.banner_hint.grid(row=2, column=0, sticky="ew", padx=16)
        # gridded only while there are slot chips: an empty CTkFrame keeps its default 200x200 size
        self.chips = ctk.CTkFrame(b, fg_color="transparent", height=1)
        self.chip_labels: list[ctk.CTkLabel] = []
        bar = ctk.CTkFrame(b, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", padx=14, pady=(6, 12))
        bar.grid_columnconfigure(0, weight=1)
        self.answer = ctk.CTkEntry(bar, height=32, fg_color=BG, border_color=LINE, text_color=TEXT,
                                   placeholder_text="type your answer…")
        self.answer.grid(row=0, column=0, sticky="ew")
        self.answer.bind("<Return>", lambda e: self._answer())
        self.answer.bind("<KP_Enter>", lambda e: self._answer())
        ctk.CTkButton(bar, text="Answer", width=76, height=32, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      text_color=ON_ACCENT, command=self._answer).grid(row=0, column=1, padx=(8, 0))
        ctk.CTkButton(bar, text="Cancel", width=76, height=32, fg_color=LINE, hover_color=RED, text_color=TEXT,
                      command=self._cancel_dialogue).grid(row=0, column=2, padx=(8, 0))

    def _build_tabs(self):
        tabs = self.tabs = ctk.CTkTabview(self, fg_color=CARD, corner_radius=16,
                                          segmented_button_fg_color=TAB_UNSEL,
                                          segmented_button_selected_color=SEG_SEL,
                                          segmented_button_selected_hover_color=SEG_SEL_HOVER,
                                          segmented_button_unselected_color=TAB_UNSEL,
                                          segmented_button_unselected_hover_color=TAB_UNSEL_HOVER,
                                          text_color=SEG_TEXT, command=self._tab_clicked)
        tabs.pack(fill="both", expand=True, padx=16, pady=(8, 16))
        for name in TABS:
            tabs.add(name)
        from xyrus.ui.calendar_tab import CalendarTab
        from xyrus.ui.commands_tab import CommandsTab
        from xyrus.ui.home_tab import HomeTab
        from xyrus.ui.apps_tab import AppsTab
        from xyrus.ui.routines_tab import RoutinesTab
        from xyrus.ui.settings_tab import SettingsTab
        self.home_tab = self._add_tab("Home", HomeTab)
        self.calendar_tab = self._add_tab("Calendar", CalendarTab)
        self.commands_tab = self._add_tab("Commands", CommandsTab)
        self.apps_tab = self._add_tab("Apps", AppsTab)
        self.routines_tab = self._add_tab("Routines", RoutinesTab)
        self.settings_tab = self._add_tab("Settings", SettingsTab)

    def _add_tab(self, name, factory):
        """A broken tab must never take the rest of the window down: it shows a notice instead."""
        tab = self.tabs.tab(name)
        try:
            w = factory(tab, self.app)
            w.pack(fill="both", expand=True)
            return w
        except Exception as e:
            log.exception("%s tab failed to build", name)
            ctk.CTkLabel(tab, text=f"{name} failed to load: {e}", text_color=RED).pack(padx=12, pady=12)
            return None

    def _tabs(self):
        return [t for t in (self.home_tab, self.calendar_tab, self.commands_tab, self.apps_tab,
                            self.routines_tab, self.settings_tab) if t is not None]

    # ------------------------------------------------------------ tabs ----- #
    def _select_tab(self, name: str, save: bool = True):
        """CTkTabview.set() grid_forgets every other tab 100 ms later, so a second switch inside that
        window (two queued requests, or a click) would leave the visible tab blank. Re-assert it after."""
        match = next((t for t in TABS if t.lower() == str(name).lower()), None)
        if match is None:
            log.warning("unknown tab %r", name)
            return
        self.tabs.set(match)
        self.after(130, self._regrid_current_tab)
        if save:
            self._save_last_tab(match)

    def _regrid_current_tab(self):
        cur = self.tabs.get()
        if cur and not self.tabs.tab(cur).winfo_manager():
            self.tabs.set(cur)

    def _tab_clicked(self):
        self.after(130, self._regrid_current_tab)
        self._save_last_tab(self.tabs.get())

    def _save_last_tab(self, name: str):
        if cfg_get(self.app.config, "window.last_tab") != name:
            cfg_set(self.app.config, "window.last_tab", name)

    def tab_ready(self, name: str) -> bool:
        """The named tab is current and actually gridded (tests poll this after a switch)."""
        return self.tabs.get() == name and self.tabs.tab(name).winfo_manager() == "grid"

    # ------------------------------------------------------------ appearance #
    def set_appearance(self, setting: str, save: bool = True):
        """Settings / "theme:<x>" message: store config ui.appearance and apply it live."""
        setting = str(setting).lower()
        if setting not in theme.APPEARANCES:
            log.warning("unknown appearance %r", setting)
            return
        self._appearance = setting
        if save and cfg_get(self.app.config, "ui.appearance") != setting:
            cfg_set(self.app.config, "ui.appearance", setting)
        theme.apply(setting)
        self._next_system_check = time.monotonic() + SYSTEM_THEME_POLL_S
        self._check_theme(force=True)

    @property
    def appearance(self) -> str:
        return self._appearance

    def _follow_windows(self):
        """"system": re-read Windows' app theme every few seconds and switch when it changed."""
        if self._appearance != "system" or time.monotonic() < self._next_system_check:
            return
        self._next_system_check = time.monotonic() + SYSTEM_THEME_POLL_S
        eff = theme.effective("system")
        if eff != theme.mode():
            ctk.set_appearance_mode(eff)

    def _check_theme(self, force: bool = False):
        eff = theme.mode()
        if not force and eff == self._effective:
            return
        self._effective = eff
        for tab in self._tabs():
            fn = getattr(tab, "on_theme", None)
            if fn is not None:
                try:
                    fn()
                except Exception:
                    log.exception("%s.on_theme failed", type(tab).__name__)

    # ------------------------------------------------------------ show/hide - #
    def show(self):
        self.deiconify()
        self.lift()
        try:
            hwnd = int(self.frame(), 16)
            from xyrus import winutil
            winutil.force_foreground(hwnd)
        except Exception:
            log.debug("force_foreground failed; falling back", exc_info=True)
            try:
                self.attributes("-topmost", True)
                self.after(200, lambda: self.attributes("-topmost", False))
                self.focus_force()
            except Exception:
                pass

    def hide(self):
        """Closing hides to the tray (one-time toast so the user knows it is still running)."""
        self._save_geometry()
        self.withdraw()
        if not cfg_get(self.app.config, "window.tray_hint_shown", False):
            # SPEC-AMBIGUITY: "one-time toast" is persisted as config window.tray_hint_shown (additive key)
            try:
                self.app.notifier.notify("Xyrus", "Still running in the tray.")
            except Exception:
                log.exception("toast failed")
            cfg_set(self.app.config, "window.tray_hint_shown", True)

    def _save_geometry(self):
        try:
            if self.state() == "normal":
                geo = self.geometry()
                if cfg_get(self.app.config, "window.geometry") != geo:
                    cfg_set(self.app.config, "window.geometry", geo)
        except Exception:
            log.exception("saving geometry failed")

    def _cancel_leftover_after(self):
        """After destroy(): drop every after() event still queued in this interpreter (tab timers,
        debounces, CTk's delayed title-bar icon). Tcl's event loop is shared per thread, so they would
        otherwise fire later ("invalid command name ...") while another window runs.
        Must run AFTER destroy(): customtkinter widgets cancel their own jobs inside destroy() and raise
        if a job was already cancelled - which aborted the destroy and leaked ~1700 USER objects a window."""
        try:
            ids = self.tk.splitlist(self.tk.call("after", "info"))
        except tk.TclError:
            return
        for job in ids:
            try:
                self.tk.call("after", "cancel", job)
            except tk.TclError:
                pass

    def close(self):
        """Stop polling, let Tk go idle, destroy, then drop leftover jobs (mainloop() returns in app.main())."""
        if self._closing:
            return
        self._save_geometry()
        self._closing = True
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        try:
            self.update_idletasks()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except Exception:
            log.exception("destroying the window failed")      # never hide a half-done destroy again
        self._cancel_leftover_after()

    quit_window = close          # ui_queue "quit"

    # ------------------------------------------------------------ actions -- #
    def _toggle_pause(self):
        paused = bool(self._snap.paused) if self._snap is not None else False
        try:
            self.app.set_paused(not paused)
        except Exception:
            log.exception("set_paused failed")

    def _stop_speaking(self):
        try:
            self.app.speaker.stop()
        except Exception:
            log.exception("speaker.stop failed")

    def _answer(self):
        text = self.answer.get().strip()
        if not text:
            return
        self.answer.delete(0, "end")
        try:
            self.app.engine.submit_text(text, "typed")
        except Exception:
            log.exception("submit_text failed")

    def _cancel_dialogue(self):
        try:
            self.app.engine.cancel_dialogue()
        except Exception:
            log.exception("cancel_dialogue failed")

    # ------------------------------------------------------------ polling -- #
    def _poll(self):
        if self._closing:
            return
        try:
            self._poll_once()
        except Exception:
            log.exception("UI poll failed")
        if not self._closing:
            self._poll_job = self.after(POLL_MS, self._poll)

    def _poll_once(self):
        app = self.app
        self._drain_queue()
        if self._closing:
            return
        self._follow_windows()
        self._check_theme()
        se = getattr(app, "show_event", None)
        if se is not None and se.poll():
            self.show()
        snap = app.engine.snapshot()
        if snap is not self._snap:
            self._snap = snap
            self._update_header(snap)
            self._update_banner(snap.dialogue)
            if self.home_tab is not None:
                self.home_tab.update_snapshot(snap)
            self._update_tray(snap)
        level = None
        cap = getattr(app, "capture", None)
        if cap is not None and self.state() == "normal":
            try:
                level = float(cap.level())
            except Exception:
                level = None
            if level is not None:
                shown = 0.0 if snap.paused else min(1.0, level * 2.5)
                if abs(shown - self._level) > 0.01:
                    self._level = shown
                    self.level.set(shown)
        for tab in (self.calendar_tab, self.commands_tab):
            if tab is not None:
                try:
                    tab.poll()
                except Exception:
                    log.exception("%s poll failed", type(tab).__name__)
        if self.settings_tab is not None:
            try:
                self.settings_tab.poll(level)
            except Exception:
                log.exception("settings poll failed")

    def _drain_queue(self):
        q = self.app.ui_queue
        for _ in range(100):
            try:
                msg = q.get_nowait()
            except queue.Empty:
                return
            try:
                self.handle_message(str(msg))
            except Exception:
                log.exception("ui message %r failed", msg)
            if self._closing:
                return

    def handle_message(self, msg: str):
        if msg == "show":
            self.show()
        elif msg == "hide":
            self.hide()
        elif msg == "quit":
            self.close()
        elif msg.startswith("theme:"):                  # voice: "theme:dark" | "theme:light" | "theme:system"
            self.set_appearance(msg.split(":", 1)[1].strip())
        elif msg.startswith("show:"):
            parts = msg.split(":")
            self.show()
            self._select_tab(parts[1])
            if parts[1].lower() == "calendar" and len(parts) > 2:
                self._show_calendar_range(parts[2], parts[3] if len(parts) > 3 else None)
        elif msg.startswith("calendar:"):              # v1 form: calendar:YYYY-MM-DD[:YYYY-MM-DD]
            parts = msg.split(":")
            self.show()
            self._select_tab("Calendar")
            self._show_calendar_range(parts[1], parts[2] if len(parts) > 2 else None)
        else:
            log.warning("unknown ui message %r", msg)

    def _show_calendar_range(self, start: str, end: str | None):
        cal = self.calendar_tab
        if cal is not None and cal.store is not None and start:
            cal.show_range(start, end or None)

    # ------------------------------------------------------------ header --- #
    def status_text(self) -> str:
        return self.status_lbl.cget("text")

    def _update_header(self, snap):
        text, color = status_text(snap)
        side = side_text(snap)
        key = (text, color, side, snap.speaking, snap.paused)
        if key == self._header_key:
            return
        self._header_key = key
        self.status_lbl.configure(text=text)
        self.status_dot.configure(text_color=color)
        self.side_lbl.configure(text=side)
        self.stop_btn.configure(state="normal" if snap.speaking else "disabled")
        self.pause_btn.configure(text="Resume" if snap.paused else "Pause")
        if snap.paused:
            self.level.set(0)
            self._level = 0.0

    def _update_banner(self, d):
        if d is None:
            if self._banner_key is not None:
                self._banner_key = None
                self.banner.pack_forget()
            return
        slots = tuple((d.slots or {}).items())
        key = (d.kind, d.state, d.question, d.hint, slots, d.seconds_left, d.parked)
        if key == self._banner_key:
            return
        first = self._banner_key is None
        self._banner_key = key
        parked = bool(d.parked)
        self.banner.configure(border_color=AMBER if parked else ACCENT, fg_color=AMBER_BG if parked else CARD)
        self.banner_caption.configure(text="Xyrus is asking · on hold" if parked else "Xyrus is asking",
                                      text_color=AMBER if parked else ACCENT)
        self.banner_q.configure(text=d.question)
        self.banner_hint.configure(text=d.hint or "")
        self.banner_count.configure(text=f"waits {d.seconds_left} s" if d.seconds_left else "")
        while len(self.chip_labels) < len(slots):
            self.chip_labels.append(ctk.CTkLabel(self.chips, text="", fg_color=LINE, corner_radius=8, height=24,
                                                 text_color=TEXT, font=ctk.CTkFont(size=12)))
        for i, chip in enumerate(self.chip_labels):
            if i < len(slots):
                name, val = slots[i]
                chip.configure(text=f"  {name}: {val}  ")
                chip.grid(row=0, column=i, padx=(2, 6), pady=2)
            else:
                chip.grid_remove()
        if slots:
            self.chips.grid(row=3, column=0, sticky="w", padx=14, pady=(4, 0))
        else:
            self.chips.grid_remove()
        if first:
            self.banner.pack(fill="x", padx=16, pady=(0, 8), before=self.tabs)

    # ------------------------------------------------------------ tray ----- #
    def _update_tray(self, snap):
        tray = getattr(self.app, "tray", None)
        if tray is None:
            return
        key = (tray_state(snap), tray_tooltip(snap))
        if key == self._tray_key:
            return
        self._tray_key = key
        try:
            tray.set_state(key[0])
            tray.set_tooltip(key[1])
        except Exception:
            log.exception("tray update failed")
