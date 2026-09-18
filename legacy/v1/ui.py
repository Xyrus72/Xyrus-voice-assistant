"""The Xyrus app window (customtkinter). Runs on the main thread; polls the
assistant's state every 150 ms so no cross-thread Tk calls are needed."""
import subprocess
import time

import customtkinter as ctk

import config

ACCENT = "#7c5cff"
ACCENT_HOVER = "#6a4ce6"
GREEN = "#3ddc84"
RED = "#ff5c5c"
MUTED = "#9a9aa8"
CARD = "#1b1b22"
BG = "#121216"

class XyrusWindow(ctk.CTk):
    def __init__(self, arc, icon, start_hidden=False):
        ctk.set_appearance_mode("dark")
        super().__init__(fg_color=BG)
        self.arc = arc
        self.icon = icon
        self._seen_events = 0

        self.title("Xyrus")
        self.geometry("960x640")
        self.minsize(720, 480)
        try:
            from arc import ICON_FILE
            self.iconbitmap(str(ICON_FILE))
        except Exception:
            pass
        self.protocol("WM_DELETE_WINDOW", self.hide)

        self._build_header()
        self._build_tabs()
        if start_hidden:
            self.withdraw()
        self.after(150, self._poll)

    # ------------------------------------------------------------ layout --- #
    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color=CARD, corner_radius=16)
        head.pack(fill="x", padx=16, pady=(16, 8))
        head.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(head, text="Xyrus", font=ctk.CTkFont(size=30, weight="bold"),
                     text_color="white").grid(row=0, column=0, padx=(20, 12), pady=(14, 0), sticky="w")
        ctk.CTkLabel(head, text="offline voice control", font=ctk.CTkFont(size=13),
                     text_color=MUTED).grid(row=1, column=0, padx=(22, 12), pady=(0, 14), sticky="w")

        right = ctk.CTkFrame(head, fg_color="transparent")
        right.grid(row=0, column=1, rowspan=2, padx=20, sticky="e")
        self.status_dot = ctk.CTkLabel(right, text="●", text_color=GREEN, font=ctk.CTkFont(size=18))
        self.status_dot.grid(row=0, column=0, padx=(0, 6))
        self.status_lbl = ctk.CTkLabel(right, text="Listening", font=ctk.CTkFont(size=15, weight="bold"))
        self.status_lbl.grid(row=0, column=1, sticky="w")
        self.timer_lbl = ctk.CTkLabel(right, text="", text_color=MUTED, font=ctk.CTkFont(size=12))
        self.timer_lbl.grid(row=1, column=0, columnspan=2, sticky="w")
        ctk.CTkLabel(right, text="mic", text_color=MUTED, font=ctk.CTkFont(size=11)).grid(row=0, column=2, padx=(24, 6))
        self.level = ctk.CTkProgressBar(right, width=140, height=10, progress_color=ACCENT)
        self.level.set(0)
        self.level.grid(row=0, column=3)
        self.pause_btn = ctk.CTkButton(right, text="Pause", width=90, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                       command=self._toggle_pause)
        self.pause_btn.grid(row=0, column=4, padx=(20, 0))

    def _build_tabs(self):
        tabs = self.tabs = ctk.CTkTabview(self, fg_color=CARD, corner_radius=16,
                                          segmented_button_selected_color=ACCENT,
                                          segmented_button_selected_hover_color=ACCENT_HOVER)
        tabs.pack(fill="both", expand=True, padx=16, pady=(8, 16))
        for name in ("Activity", "Calendar", "Commands", "Apps", "Settings"):
            tabs.add(name)
        self._build_activity(tabs.tab("Activity"))
        self._build_calendar(tabs.tab("Calendar"))
        self._build_commands(tabs.tab("Commands"))
        self._build_apps(tabs.tab("Apps"))
        self._build_settings(tabs.tab("Settings"))

    def _build_activity(self, tab):
        self.feed = ctk.CTkTextbox(tab, font=ctk.CTkFont(family="Consolas", size=13), fg_color=BG,
                                   wrap="word", activate_scrollbars=True)
        self.feed.pack(fill="both", expand=True, padx=8, pady=8)
        self.feed.tag_config("heard", foreground=MUTED)
        self.feed.tag_config("cmd", foreground=ACCENT)
        self.feed.tag_config("say", foreground="white")
        self.feed.tag_config("info", foreground=GREEN)
        self.feed.configure(state="disabled")
        ctk.CTkLabel(tab, text='Say "Xyrus" then a command. Everything heard shows up here.',
                     text_color=MUTED).pack(anchor="w", padx=12, pady=(0, 6))

    def _build_calendar(self, tab):
        """Month grid + day agenda + quick-add (calendar_tab.py). A broken calendar must never take
        the rest of the window down, so a failure here leaves a notice instead."""
        self.calendar_tab = None
        try:
            from calendar_tab import CalendarTab
            self.calendar_tab = CalendarTab(tab, getattr(self.arc, "calendar", None),
                                            now=getattr(self.arc, "now", None))
            self.calendar_tab.pack(fill="both", expand=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            ctk.CTkLabel(tab, text=f"Calendar failed to load: {e}", text_color=RED).pack(padx=12, pady=12)

    def _build_commands(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(8, 0))
        self.cmd_count = ctk.CTkLabel(top, text="", text_color=MUTED)
        self.cmd_count.pack(side="left", padx=4)
        self.cmd_filter = ctk.CTkEntry(top, width=220, placeholder_text="filter…")
        self.cmd_filter.pack(side="right", padx=4)
        self.cmd_filter.bind("<KeyRelease>", lambda e: self._render_commands())
        self.cmd_frame = ctk.CTkScrollableFrame(tab, fg_color=BG)
        self.cmd_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self._render_commands()

    def _command_sections(self):
        """Every command and every way to say it, straight from the engine's table."""
        import arc as arc_module
        from arc import COMMANDS, COMMAND_INFO
        order = ["Power", "Calendar & to-dos", "Sound & media", "Apps & windows", "Timers", "Info & tools", "Other"]
        sections = {s: [] for s in order}
        sections["Calendar & to-dos"] += list(getattr(arc_module, "CALENDAR_HELP", []))
        sections["Sound & media"].append((
            "play <song name>",
            "finds the song on YouTube and plays it, e.g. play alone, play shape of you by ed sheeran"))
        sections["Apps & windows"].append((
            "open / launch / start <app>",
            "apps: " + ", ".join(config.get("apps", {})) + "  (edit in the Apps tab)"))
        for key, phrases in COMMANDS.items():
            section, what = COMMAND_INFO.get(key, ("Other", ""))
            sections.setdefault(section, []).append((" / ".join(phrases), what))
        sections["Timers"] = [
            ("set a timer for <n> minutes / seconds / hours", "any number: seven minutes, twenty five seconds, half an hour, two hours"),
            ("cancel timer / stop timer", "works without the wake word while a timer is running"),
        ]
        sections["Other"] += [
            ("(just the name)", 'replies "Waiting for your command, sir." and listens for 15 s'),
            ("(say it your own way)", "commands understand meaning: 'lower the volume', 'decrease the sound', "
                                      "'make it quieter' and 'turn it down a bit' all work; 'turn off the sound' "
                                      "mutes and never shuts the PC down"),
        ]
        return [(s, rows) for s, rows in sections.items() if rows]

    def _render_commands(self):
        for w in self.cmd_frame.winfo_children():
            w.destroy()
        q = self.cmd_filter.get().strip().lower()
        n_cmd = n_phr = 0
        for section, rows in self._command_sections():
            rows = [r for r in rows if not q or q in r[0].lower() or q in r[1].lower()]
            if not rows:
                continue
            ctk.CTkLabel(self.cmd_frame, text=section, font=ctk.CTkFont(size=15, weight="bold"),
                         text_color=ACCENT).pack(anchor="w", padx=12, pady=(12, 4))
            for phrase, what in rows:
                n_cmd += 1
                n_phr += phrase.count(" / ") + 1
                row = ctk.CTkFrame(self.cmd_frame, fg_color="transparent")
                row.pack(fill="x", padx=12, pady=1)
                ctk.CTkLabel(row, text=f'"Xyrus, {phrase}"', width=360, anchor="w", justify="left",
                             wraplength=350, font=ctk.CTkFont(family="Consolas", size=13)).pack(side="left")
                ctk.CTkLabel(row, text=what, text_color=MUTED, anchor="w", justify="left",
                             wraplength=380).pack(side="left", padx=8)
        self.cmd_count.configure(text=f"{n_cmd} commands · {n_phr} ways to say them")

    def _build_apps(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(8, 0))
        ctk.CTkLabel(top, text='Say "Xyrus, open <name>".  Target can be an .exe, a URL, or a URI like spotify:',
                     text_color=MUTED).pack(side="left", padx=4)
        ctk.CTkButton(top, text="Save", width=80, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=self._save_apps).pack(side="right", padx=4)
        ctk.CTkButton(top, text="+ Add", width=80, fg_color="#2b2b36", command=lambda: self._add_app_row("", "")
                      ).pack(side="right", padx=4)
        self.apps_frame = ctk.CTkScrollableFrame(tab, fg_color=BG)
        self.apps_frame.pack(fill="both", expand=True, padx=8, pady=8)
        hdr = ctk.CTkFrame(self.apps_frame, fg_color="transparent")
        hdr.pack(fill="x", padx=4)
        ctk.CTkLabel(hdr, text="say", width=180, anchor="w", text_color=MUTED).pack(side="left")
        ctk.CTkLabel(hdr, text="opens", anchor="w", text_color=MUTED).pack(side="left", padx=8)
        self.app_rows = []
        for name, target in config.get("apps", {}).items():
            self._add_app_row(name, target)
        self.apps_msg = ctk.CTkLabel(tab, text="", text_color=GREEN)
        self.apps_msg.pack(anchor="w", padx=12, pady=(0, 6))

    def _add_app_row(self, name, target):
        row = ctk.CTkFrame(self.apps_frame, fg_color="transparent")
        row.pack(fill="x", pady=2, padx=4)
        e1 = ctk.CTkEntry(row, width=180, placeholder_text="name")
        e1.pack(side="left")
        e2 = ctk.CTkEntry(row, placeholder_text="chrome  /  https://…  /  spotify:")
        e2.pack(side="left", fill="x", expand=True, padx=8)
        if name:
            e1.insert(0, name)
        if target:
            e2.insert(0, target)
        btn = ctk.CTkButton(row, text="✕", width=32, fg_color="#2b2b36", hover_color=RED,
                            command=lambda: self._remove_app_row(row))
        btn.pack(side="left")
        self.app_rows.append((row, e1, e2))

    def _remove_app_row(self, row):
        self.app_rows = [r for r in self.app_rows if r[0] is not row]
        row.destroy()

    def _save_apps(self):
        apps = {}
        for _, e1, e2 in self.app_rows:
            n, t = e1.get().strip().lower(), e2.get().strip()
            if n and t:
                apps[n] = t
        config.set_("apps", apps)
        self.arc.grammar_dirty = True
        self._render_commands()
        self.apps_msg.configure(text=f"Saved {len(apps)} apps — Xyrus now knows the new names.")
        self.after(4000, lambda: self.apps_msg.configure(text=""))

    def _build_settings(self, tab):
        from arc import autostart_enabled, set_autostart, LOG_FILE, BASE

        box = ctk.CTkFrame(tab, fg_color=BG, corner_radius=12)
        box.pack(fill="x", padx=8, pady=8)

        def switch(text, sub, initial, cb):
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=8)
            sw = ctk.CTkSwitch(row, text=text, font=ctk.CTkFont(size=14), progress_color=ACCENT, command=lambda: cb(sw))
            sw.pack(side="left")
            if initial:
                sw.select()
            ctk.CTkLabel(row, text=sub, text_color=MUTED).pack(side="left", padx=12)
            return sw

        self.sw_auto = switch("Start with Windows", "runs hidden in the tray after every boot",
                              autostart_enabled(), lambda sw: set_autostart(bool(sw.get())))
        switch("Voice replies", "speak answers out loud (timers always speak)",
               config.get("voice_replies", True), lambda sw: config.set_("voice_replies", bool(sw.get())))
        switch("Confirm before shutdown / restart", 'ask "yes?" first',
               config.get("confirm_shutdown", True), lambda sw: config.set_("confirm_shutdown", bool(sw.get())))

        btns = ctk.CTkFrame(tab, fg_color="transparent")
        btns.pack(fill="x", padx=8, pady=8)
        ctk.CTkButton(btns, text="Test voice", fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=lambda: self.arc.say("Hello, I'm Xyrus. I'm listening.", force=True)).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="Open log", fg_color="#2b2b36",
                      command=lambda: subprocess.Popen(["notepad", str(LOG_FILE)])).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="Open folder", fg_color="#2b2b36",
                      command=lambda: subprocess.Popen(["explorer", str(BASE)])).pack(side="left", padx=8)
        ctk.CTkLabel(tab, text="Closing this window keeps Xyrus running in the tray. Quit from the tray icon.",
                     text_color=MUTED).pack(anchor="w", padx=16, pady=(12, 0))

    # ------------------------------------------------------------ actions -- #
    def _toggle_pause(self):
        self.arc.paused = not self.arc.paused
        from arc import make_icon_image, NAME
        self.icon.icon = make_icon_image(active=not self.arc.paused)
        self.icon.title = f"{NAME} (paused)" if self.arc.paused else f"{NAME} - listening"

    def _select_tab(self, name):
        """CTkTabview.set() grid_forgets every other tab 100 ms later, so a second switch inside that
        window (two queued requests, or a click) would leave the visible tab blank. Re-assert it after."""
        self.tabs.set(name)
        self.after(130, self._regrid_current_tab)

    def _regrid_current_tab(self):
        cur = self.tabs.get()
        if cur and not self.tabs.tab(cur).winfo_manager():
            self.tabs.set(cur)

    def show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide(self):
        self.withdraw()

    # ------------------------------------------------------------ polling -- #
    def _poll(self):
        arc = self.arc
        # tray requests
        try:
            while True:
                req = arc.ui_queue.get_nowait()
                if req == "show":
                    self.show()
                elif req.startswith("calendar:"):
                    # "calendar:YYYY-MM-DD" or "calendar:YYYY-MM-DD:YYYY-MM-DD" (a spoken answer overflowed)
                    self.show()
                    self._select_tab("Calendar")
                    if self.calendar_tab is not None and self.calendar_tab.store is not None:
                        days = req.split(":")[1:3]
                        self.calendar_tab.show_range(days[0], days[1] if len(days) > 1 else None)
                elif req.startswith("show:"):
                    self.show()
                    try:
                        self._select_tab(req.split(":", 1)[1])
                    except Exception:
                        pass
                elif req == "quit":
                    self.destroy()
                    return
        except Exception:
            pass

        # status
        if arc.paused:
            self.status_dot.configure(text_color=MUTED)
            self.status_lbl.configure(text="Paused · mic off" if arc.status == "paused" else "Pausing…")
            self.pause_btn.configure(text="Resume")
        elif arc.status == "listening":
            self.status_dot.configure(text_color=GREEN); self.status_lbl.configure(text="Listening")
            self.pause_btn.configure(text="Pause")
        else:
            self.status_dot.configure(text_color=RED); self.status_lbl.configure(text=arc.status)
        self.level.set(min(1.0, arc.level * 3))
        arc.level *= 0.85
        rem = arc.timer_remaining()
        self.timer_lbl.configure(text=f"timer: {rem // 60:02d}:{rem % 60:02d}" if rem else "")
        if arc.pending_confirm and time.time() < arc.pending_confirm_until:
            self.status_lbl.configure(text=f"Confirm {arc.pending_confirm}?")

        # feed
        events = list(arc.events)
        if len(events) < self._seen_events:
            self._seen_events = 0
        new = events[self._seen_events:]
        if new:
            self.feed.configure(state="normal")
            for ts, kind, text in new:
                prefix = {"heard": "heard ", "cmd": "  →  ", "say": "  💬  ", "info": "  ✓  "}.get(kind, "")
                self.feed.insert("end", f"{ts}  {prefix}{text}\n", kind)
            self.feed.see("end")
            self.feed.configure(state="disabled")
            self._seen_events = len(events)

        # calendar (store changes from voice arrive via store.version)
        if self.calendar_tab is not None:
            try:
                self.calendar_tab.poll()
            except Exception:
                import traceback
                traceback.print_exc()

        self.after(150, self._poll)
