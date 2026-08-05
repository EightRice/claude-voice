#!/usr/bin/env python3
"""
Voice Overlay — always-on-top status window for the voice service.

Shows:
  - Recording/transcribing/speaking state
  - Connected Claude instances (click to unmute)
  - Tool activity
  - Audio device selection
  - Errors

Launched by voice_service.py. Communicates via UDP.
"""

import json
import socket
import sys
import time
import threading
import tkinter as tk
from tkinter import ttk


# ── Colors ────────────────────────────────────────────────────

BG           = "#1a1a2e"
BG_LIGHT     = "#222244"
BG_INSTANCE  = "#1e1e38"
FG           = "#e0e0e0"
FG_DIM       = "#888899"
ACCENT       = "#4fc3f7"   # light blue
RECORDING    = "#ef5350"   # red
PROCESSING   = "#ffa726"   # orange
SPEAKING     = "#66bb6a"   # green
MUTED_COLOR  = "#78909c"   # grey-blue
TOOL_COLOR   = "#ce93d8"   # purple
LOADING      = "#29b6f6"   # light blue
UNMUTED_DOT  = "#66bb6a"   # green
MUTED_DOT    = "#555566"   # dim
ACTIVE_DOT   = "#ffa726"   # orange — instance is working
ACTIVE_FG    = "#ffa726"   # orange text for active instance
PAUSED_COLOR = "#ffee58"   # yellow — playback paused
ALARM_BG     = "#b00020"   # deep red — security alarm panel
ALARM_FG     = "#ff5252"   # red text — instance with outstanding alerts


class VoiceOverlay:
    def __init__(self, port):
        self.port = port
        self._state = "idle"
        self._pulse_on = True
        self._last_text = ""
        self._last_tool = ""
        self._backend = ""
        self._key = ""
        self._error = ""
        self._loading_models = set()
        self._cmd_port = 0
        self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._output_devs = []
        self._input_devs = []
        self._settings_visible = False
        self._instances = []
        self._last_msg_time = time.monotonic()
        self._heartbeat_timeout = 30

        self._build_ui()
        self._start_listener()
        self._pulse_tick()
        self._heartbeat_check()
        self.root.mainloop()

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Voice")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.overrideredirect(True)

        self._w = 340
        self._base_h = 140
        sx = self.root.winfo_screenwidth() - self._w - 16
        sy = self.root.winfo_screenheight() - self._base_h - 48
        self.root.geometry(f"{self._w}x{self._base_h}+{sx}+{sy}")

        self.root.bind("<Button-1>", self._start_drag)
        self.root.bind("<B1-Motion>", self._do_drag)

        # Main frame
        frame = tk.Frame(self.root, bg=BG, padx=12, pady=8)
        frame.pack(fill=tk.BOTH, expand=True)

        # Row 1: status indicator + state text + gear + close
        row1 = tk.Frame(frame, bg=BG)
        row1.pack(fill=tk.X, pady=(0, 4))

        self.indicator = tk.Canvas(row1, width=14, height=14, bg=BG,
                                   highlightthickness=0)
        self.indicator.pack(side=tk.LEFT, padx=(0, 8))
        self._dot = self.indicator.create_oval(2, 2, 12, 12, fill=FG_DIM, outline="")

        self.state_label = tk.Label(row1, text="Idle", font=("Consolas", 11, "bold"),
                                    fg=FG, bg=BG, anchor="w")
        self.state_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        close_btn = tk.Label(row1, text="\u00d7", font=("Consolas", 14),
                             fg=FG_DIM, bg=BG, cursor="hand2")
        close_btn.pack(side=tk.RIGHT, padx=(4, 0))
        close_btn.bind("<Button-1>", lambda e: self.root.destroy())

        self._gear = tk.Label(row1, text="\u2699", font=("Consolas", 13),
                              fg=FG_DIM, bg=BG, cursor="hand2")
        self._gear.pack(side=tk.RIGHT, padx=(4, 0))
        self._gear.bind("<Button-1>", lambda e: self._toggle_settings())

        # Tool-narration toggle \u2014 silences the announcer voice while leaving
        # response speech alone. Lit = announcements on.
        self._narrate_on = True
        self._narrate_btn = tk.Label(row1, text="\U0001f5e3", font=("Consolas", 12),
                                     fg=SPEAKING, bg=BG, cursor="hand2")
        self._narrate_btn.pack(side=tk.RIGHT, padx=(4, 0))
        self._narrate_btn.bind(
            "<Button-1>",
            lambda e: self._send_command(action="set_narrate",
                                         enabled=not self._narrate_on))

        # Spoken-security-alarm toggle. Silences ONLY the speech \u2014 incidents
        # still hit the log, the durable record and the panel below.
        self._alarm_voice_on = True
        self._alarm_btn = tk.Label(row1, text="\U0001f514", font=("Consolas", 12),
                                   fg=SPEAKING, bg=BG, cursor="hand2")
        self._alarm_btn.pack(side=tk.RIGHT, padx=(4, 0))
        self._alarm_btn.bind(
            "<Button-1>",
            lambda e: self._send_command(action="set_alarm_voice",
                                         enabled=not self._alarm_voice_on))

        # Row 2: detail text
        self.detail_label = tk.Label(frame, text="", font=("Consolas", 9),
                                     fg=FG_DIM, bg=BG, anchor="w")
        self.detail_label.pack(fill=tk.X, pady=(0, 4))

        # Row 3: error
        self.error_label = tk.Label(frame, text="", font=("Consolas", 9),
                                    fg=RECORDING, bg=BG, anchor="w")
        self.error_label.pack(fill=tk.X, pady=(0, 4))

        # Persistent SECURITY ALARM panel — one row per incident (agent /
        # secret names / when), newest first. Distinct from error_label: NOT
        # cleared by any other state. Each row has an 'x' to acknowledge that
        # incident; 'clear all' clears them together. Survives restarts (the
        # service re-asserts the panel). Not packed until an incident exists.
        self._incidents = []   # list of {id, names, instance, when}
        self.alarm_panel = tk.Frame(frame, bg=ALARM_BG)
        self._alarm_rows = []  # widgets to destroy on rebuild

        # Instance list
        self._instance_canvas = tk.Canvas(frame, bg=BG, highlightthickness=0,
                                           height=0)
        self._instance_canvas.pack(fill=tk.X, pady=(0, 4))
        self._instance_frame = tk.Frame(self._instance_canvas, bg=BG)
        self._instance_canvas_win = self._instance_canvas.create_window(
            (0, 0), window=self._instance_frame, anchor="nw",
            width=self._w - 24)
        self._instance_frame.bind("<Configure>", self._on_instance_frame_configure)
        self._instance_canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self._instance_widgets = []

        # Settings panel (hidden by default)
        self._settings_frame = tk.Frame(frame, bg=BG_LIGHT, padx=8, pady=6)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TCombobox",
                        fieldbackground=BG_LIGHT, background=BG_LIGHT,
                        foreground=FG, selectbackground=BG_LIGHT,
                        selectforeground=FG, arrowcolor=FG_DIM)
        style.map("Dark.TCombobox",
                  fieldbackground=[("readonly", BG_LIGHT)],
                  foreground=[("readonly", FG)])

        tk.Label(self._settings_frame, text="Output", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_LIGHT, anchor="w").pack(fill=tk.X)
        self._out_var = tk.StringVar()
        self._out_combo = ttk.Combobox(self._settings_frame, textvariable=self._out_var,
                                        state="readonly", style="Dark.TCombobox",
                                        font=("Consolas", 8))
        self._out_combo.pack(fill=tk.X, pady=(0, 4))
        self._out_combo.bind("<<ComboboxSelected>>", self._on_output_changed)

        tk.Label(self._settings_frame, text="Input", font=("Consolas", 8),
                 fg=FG_DIM, bg=BG_LIGHT, anchor="w").pack(fill=tk.X)
        self._in_var = tk.StringVar()
        self._in_combo = ttk.Combobox(self._settings_frame, textvariable=self._in_var,
                                       state="readonly", style="Dark.TCombobox",
                                       font=("Consolas", 8))
        self._in_combo.pack(fill=tk.X)
        self._in_combo.bind("<<ComboboxSelected>>", self._on_input_changed)

        # Info bar
        self.info_label = tk.Label(frame, text="", font=("Consolas", 8),
                                   fg=FG_DIM, bg=BG_LIGHT, anchor="w",
                                   padx=6, pady=2)
        self.info_label.pack(fill=tk.X, side=tk.BOTTOM)

    def _rebuild_instance_list(self):
        for w, _, _ in self._instance_widgets:
            w.destroy()
        self._instance_widgets = []

        for inst in self._instances:
            sid = inst.get("session_id", "")
            name = inst.get("name", sid[:8])
            muted = inst.get("muted", True)
            active = inst.get("active", False)

            row = tk.Frame(self._instance_frame, bg=BG_INSTANCE, padx=6, pady=3,
                           cursor="hand2")
            row.pack(fill=tk.X, pady=1)

            dot = tk.Canvas(row, width=10, height=10, bg=BG_INSTANCE,
                            highlightthickness=0)
            dot.pack(side=tk.LEFT, padx=(0, 6))
            if active:
                dot_color = ACTIVE_DOT
            elif not muted:
                dot_color = UNMUTED_DOT
            else:
                dot_color = MUTED_DOT
            dot.create_oval(1, 1, 9, 9, fill=dot_color, outline="")

            display = name
            if not muted:
                display += "  \u25c0"
            if active:
                display += "  \u2026"

            if active:
                label_fg = ACTIVE_FG
            elif not muted:
                label_fg = FG
            else:
                label_fg = FG_DIM
            label = tk.Label(row, text=display, font=("Consolas", 9),
                             fg=label_fg, bg=BG_INSTANCE, anchor="w")
            label.pack(side=tk.LEFT, fill=tk.X, expand=True)

            def _on_click(e, s=sid, m=muted):
                if m:
                    self._send_command(action="unmute", session_id=s)
                else:
                    self._send_command(action="mute", session_id=s)
            row.bind("<Button-1>", _on_click)
            dot.bind("<Button-1>", _on_click)
            label.bind("<Button-1>", _on_click)

            self._instance_widgets.append((row, dot, label))

        self._resize_window()

    def _on_instance_frame_configure(self, event=None):
        self._instance_canvas.configure(scrollregion=self._instance_canvas.bbox("all"))

    def _on_mousewheel(self, event):
        self._instance_canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def _resize_window(self):
        n_instances = len(self._instances)
        row_h = 22
        instance_height = n_instances * row_h
        max_instance_height = 10 * row_h
        canvas_h = min(instance_height, max_instance_height)
        self._instance_canvas.configure(height=canvas_h)

        settings_height = 100 if self._settings_visible else 0

        # Alarm panel height: header + one row per incident (only when shown).
        # Without this term the panel would push the instance list off-screen.
        alarm_row_h = 20
        n_incidents = len(self._incidents)
        alarm_height = (alarm_row_h * (n_incidents + 1) + 8) if n_incidents else 0

        total_h = self._base_h + canvas_h + settings_height + alarm_height

        screen_h = self.root.winfo_screenheight()
        max_h = screen_h - 48
        total_h = min(total_h, max_h)

        x, y = self.root.winfo_x(), self.root.winfo_y()
        old_h = self.root.winfo_height()
        new_y = y + (old_h - total_h)
        new_y = max(8, new_y)
        self.root.geometry(f"{self._w}x{total_h}+{x}+{new_y}")

    def _toggle_settings(self):
        self._settings_visible = not self._settings_visible
        if self._settings_visible:
            self._settings_frame.pack(fill=tk.X, pady=(0, 4), before=self.info_label)
            self._gear.config(fg=ACCENT)
        else:
            self._settings_frame.pack_forget()
            self._gear.config(fg=FG_DIM)
        self._resize_window()

    def _on_output_changed(self, _event=None):
        idx = self._out_combo.current()
        if 0 <= idx < len(self._output_devs):
            self._send_command(action="set_output_device",
                               device_id=self._output_devs[idx]["id"])

    def _on_input_changed(self, _event=None):
        idx = self._in_combo.current()
        if 0 <= idx < len(self._input_devs):
            self._send_command(action="set_input_device",
                               device_id=self._input_devs[idx]["id"])

    def _send_command(self, **kwargs):
        if not self._cmd_port:
            return
        try:
            data = json.dumps(kwargs).encode("utf-8")
            self._cmd_sock.sendto(data, ("127.0.0.1", self._cmd_port))
        except Exception:
            pass

    def _populate_devices(self, devices, current_output, current_input):
        self._output_devs = devices.get("outputs", [])
        self._input_devs = devices.get("inputs", [])

        out_names, out_select = [], 0
        for i, d in enumerate(self._output_devs):
            suffix = " *" if d.get("default") else ""
            out_names.append(f"{d['name'][:45]}{suffix}")
            if current_output is not None and d["id"] == current_output:
                out_select = i
            elif current_output is None and d.get("default"):
                out_select = i

        in_names, in_select = [], 0
        for i, d in enumerate(self._input_devs):
            suffix = " *" if d.get("default") else ""
            in_names.append(f"{d['name'][:45]}{suffix}")
            if current_input is not None and d["id"] == current_input:
                in_select = i
            elif current_input is None and d.get("default"):
                in_select = i

        self._out_combo["values"] = out_names
        if out_names:
            self._out_combo.current(out_select)
        self._in_combo["values"] = in_names
        if in_names:
            self._in_combo.current(in_select)

    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_drag(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _start_listener(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", self.port))
        t = threading.Thread(target=self._listen, daemon=True)
        t.start()

    def _listen(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(65535)
                self._last_msg_time = time.monotonic()
                msg = json.loads(data.decode("utf-8", errors="replace"))
                if msg.get("state") == "shutdown":
                    self.root.after(0, self.root.destroy)
                    return
                self.root.after(0, self._handle, msg)
            except Exception:
                pass

    def _heartbeat_check(self):
        self.root.after(10000, self._heartbeat_check)

    def _handle(self, msg):
        state = msg.get("state", "")

        if state == "init":
            self._backend = msg.get("backend", "")
            self._key = msg.get("key", "")
            self._cmd_port = msg.get("cmd_port", 0)
            # Sync the toggle buttons with the service's actual state so a
            # --no-narrate / --no-alarm-voice start (or an overlay relaunch
            # after toggling) isn't shown stale. Missing field defaults to ON
            # so a dropped flag can never silently mute the alarm.
            self._set_narrate_indicator(msg.get("narrate", True))
            self._set_alarm_voice_indicator(msg.get("alarm_voice", True))
            devices = msg.get("devices", {})
            if devices:
                self._populate_devices(
                    devices,
                    devices.get("current_output"),
                    devices.get("current_input"),
                )
            self._update_info()
            return

        if state == "instances":
            self._instances = msg.get("instances", [])
            self._rebuild_instance_list()
            return

        if state == "loading":
            model = msg.get("model", "model")
            self._loading_models.add(model)
            names = ", ".join(sorted(self._loading_models))
            self._set_state("loading", f"Loading {names}...", LOADING)
            self.detail_label.config(text="PTT will work once models are ready",
                                     fg=FG_DIM)
            return

        if state == "ready":
            model = msg.get("model", "model")
            self._loading_models.discard(model)
            if self._loading_models:
                names = ", ".join(sorted(self._loading_models))
                self._set_state("loading", f"Loading {names}...", LOADING)
            else:
                self._set_state("idle", "Ready", SPEAKING)
                self.detail_label.config(text="All models loaded", fg=SPEAKING)
                self.root.after(3000, lambda: (
                    self._set_state("idle", "Idle", FG_DIM)
                    if self._state == "idle" else None,
                    self.detail_label.config(text="")
                    if self._state == "idle" else None,
                ))
            return

        if state == "recording":
            self._set_state("recording", "Recording...", RECORDING)
            self._error = ""
            self.error_label.config(text="")
        elif state == "transcribing":
            self._set_state("transcribing", "Transcribing...", PROCESSING)
        elif state == "transcribed":
            text = msg.get("text", "")
            self._last_text = text
            self._set_state("idle", "Idle", FG_DIM)
            display = text[:42] + "\u2026" if len(text) > 42 else text
            self.detail_label.config(
                text=f'\u201c{display}\u201d' if text else "", fg=FG)
        elif state == "speaking":
            self._set_state("speaking", "Speaking...", SPEAKING)
        elif state == "speaking_done":
            self._set_state("idle", "Idle", FG_DIM)
        elif state == "paused":
            self._set_state("paused", "Paused", PAUSED_COLOR)
        elif state == "resumed":
            self._set_state("speaking", "Speaking...", SPEAKING)
        elif state == "tool":
            name = msg.get("name", "?")
            summary = msg.get("summary", "")
            inst_name = msg.get("instance", "")
            prefix = f"[{inst_name}] " if inst_name else ""
            self._last_tool = f"{name}: {summary}"
            self.detail_label.config(
                text=f"\u2699 {prefix}{self._last_tool[:100]}", fg=TOOL_COLOR)
            self.indicator.itemconfig(self._dot, fill=TOOL_COLOR)
            self.root.after(400, self._restore_dot)
        elif state == "error":
            self._error = msg.get("message", "Unknown error")
            self.error_label.config(text=f"\u26a0 {self._error}")
        elif state == "skipped":
            did = msg.get("did_skip", True)
            label = "Skipped \u23ed" if did else "Nothing to skip"
            color = SPEAKING if did else FG_DIM
            self._set_state("skipped", label, color)
            # Revert to idle shortly after, unless another state arrives first.
            self.root.after(900, lambda: (
                self._set_state("idle", "Idle", FG_DIM)
                if self._state == "skipped" else None))
        elif state == "narrate":
            # Service is the source of truth: it echoes the resulting state
            # whether the change came from the button or the hotkey.
            self._set_narrate_indicator(msg.get("narrate", True))
        elif state == "alarm_voice":
            self._set_alarm_voice_indicator(msg.get("alarm_voice", True))
        elif state == "alarm_panel":
            self._render_alarm_panel(msg.get("incidents", []))
        elif state == "idle":
            self._set_state("idle", "Idle", FG_DIM)

    def _fmt_when(self, when):
        """Relative time for an incident timestamp (e.g. 'just now', '5m ago')."""
        if not when:
            return ""
        try:
            delta = max(0, int(time.time() - float(when)))
        except (TypeError, ValueError):
            return ""
        if delta < 10:
            return "just now"
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        if delta < 86400:
            return f"{delta // 3600}h ago"
        return f"{delta // 86400}d ago"

    def _render_alarm_panel(self, incidents):
        """Render the per-incident security panel. Newest first (service sorts).

        Each row: 🚨 AGENT (bold) · secret names · relative time · [x clear].
        A 'Clear all' link at the top clears every incident at once.
        """
        self._incidents = incidents or []
        # Tear down old rows.
        for w in self._alarm_rows:
            w.destroy()
        self._alarm_rows = []

        if not self._incidents:
            if self.alarm_panel.winfo_ismapped():
                self.alarm_panel.pack_forget()
            self._resize_window()
            return

        # Header with Clear-all.
        header = tk.Frame(self.alarm_panel, bg=ALARM_BG)
        header.pack(fill=tk.X, padx=4, pady=(3, 1))
        self._alarm_rows.append(header)
        tk.Label(header, text="\U0001f6a8 SECURITY ALARM", font=("Consolas", 10, "bold"),
                 fg="#ffffff", bg=ALARM_BG, anchor="w").pack(side=tk.LEFT)
        clear_all = tk.Label(header, text="clear all", font=("Consolas", 8, "underline"),
                             fg="#ffd0d0", bg=ALARM_BG, cursor="hand2")
        clear_all.pack(side=tk.RIGHT)
        clear_all.bind("<Button-1>",
                       lambda e: self._send_command(action="clear_alarms"))

        # One row per incident.
        for inc in self._incidents:
            row = tk.Frame(self.alarm_panel, bg=ALARM_BG)
            row.pack(fill=tk.X, padx=4, pady=1)
            self._alarm_rows.append(row)

            agent = inc.get("instance", "?") or "?"
            names = ", ".join(inc.get("names", [])) or "?"
            rel = self._fmt_when(inc.get("when"))
            cid = inc.get("id", "")

            close = tk.Label(row, text="✕", font=("Consolas", 9, "bold"),
                             fg="#ffd0d0", bg=ALARM_BG, cursor="hand2")
            close.pack(side=tk.RIGHT, padx=(4, 0))
            close.bind("<Button-1>",
                       lambda e, i=cid: self._send_command(action="clear_alarm", id=i))

            tk.Label(row, text=agent, font=("Consolas", 9, "bold"),
                     fg="#ffffff", bg=ALARM_BG, anchor="w").pack(side=tk.LEFT)
            detail = f"  {names}"
            if rel:
                detail += f"  · {rel}"
            tk.Label(row, text=detail, font=("Consolas", 9),
                     fg="#ffe0e0", bg=ALARM_BG, anchor="w").pack(
                         side=tk.LEFT, fill=tk.X, expand=True)

        if not self.alarm_panel.winfo_ismapped():
            # Place the panel just below the error row (high visual priority).
            self.alarm_panel.pack(fill=tk.X, pady=(0, 4), after=self.error_label)
        self._resize_window()

    def _set_alarm_voice_indicator(self, on):
        """Reflect spoken-alarm state. Bell icon swaps to a crossed bell when
        muted, so a silenced alarm is visibly obvious rather than forgotten."""
        self._alarm_voice_on = bool(on)
        self._alarm_btn.config(
            text="\U0001f514" if self._alarm_voice_on else "\U0001f515",
            fg=SPEAKING if self._alarm_voice_on else FG_DIM)
        self.detail_label.config(
            text="Alarm voice ON" if self._alarm_voice_on
            else "Alarm voice OFF (still logged + shown)")

    def _set_narrate_indicator(self, on):
        """Reflect tool-narration state on the toggle button + detail line."""
        self._narrate_on = bool(on)
        self._narrate_btn.config(fg=SPEAKING if self._narrate_on else FG_DIM)
        self.detail_label.config(
            text="Tool voice ON" if self._narrate_on else "Tool voice OFF")

    def _set_state(self, state, label, color):
        self._state = state
        self.state_label.config(text=label, fg=color)
        self.indicator.itemconfig(self._dot, fill=color)

    def _restore_dot(self):
        colors = {
            "idle": FG_DIM, "recording": RECORDING, "transcribing": PROCESSING,
            "speaking": SPEAKING, "muted": MUTED_COLOR, "loading": LOADING,
            "paused": PAUSED_COLOR,
        }
        self.indicator.itemconfig(self._dot, fill=colors.get(self._state, FG_DIM))

    def _update_info(self):
        parts = []
        if self._backend:
            parts.append(self._backend)
        n = len(self._instances)
        if n:
            parts.append(f"{n} instance{'s' if n != 1 else ''}")
        if self._key:
            keys = self._key if isinstance(self._key, list) else [self._key]
            parts.append(" / ".join(f"[{k.upper()}]" for k in keys))
        self.info_label.config(text="  \u2502  ".join(parts))

    def _pulse_tick(self):
        if self._state in ("recording", "loading", "paused"):
            color_map = {"recording": RECORDING, "loading": LOADING,
                         "paused": PAUSED_COLOR}
            color = color_map[self._state]
            self._pulse_on = not self._pulse_on
            if self._pulse_on:
                self.indicator.itemconfig(self._dot, fill=color)
            else:
                self.indicator.itemconfig(self._dot, fill=BG)
        self.root.after(500, self._pulse_tick)


def main():
    if len(sys.argv) < 2:
        print("Usage: voice_overlay.py <port>")
        sys.exit(1)
    port = int(sys.argv[1])
    VoiceOverlay(port)


if __name__ == "__main__":
    main()
