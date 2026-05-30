"""
StealthAI v8 — Enhanced Meeting & Productivity Assistant
=========================================================
UI Layout (resizable):
  ① Header bar  (title + ☰ toggle icon bar + — ✕ + theme switch)
  ② Icon bar    (hidden by default)
  ③ Input box   ← TOP
  ④ AI response ← fills middle (expandable)
  ⑤ Transcript  ← bottom (resizable via split pane)

Icon bar (☰ to show/hide):
  🔑  API Key      🤖  Provider      🔆  Opacity
  ✂️  Snip         📷  Full Screen   👂  Meeting
  🎤  Voice Input  🙈  Hide          🗑  Clear
  ⌨️  Shortcuts    🪶  Compact Mode  🌓  Theme

GLOBAL HOTKEYS (unchanged):
  Ctrl+Shift+H  →  Hide / Show
  Ctrl+Shift+S  →  Snip region
  Ctrl+Shift+F  →  Full screen capture
  Ctrl+Shift+M  →  Meeting listener

Improvements included:
  - Local Whisper STT (faster-whisper)
  - Noise suppression (noisereduce)
  - VAD fine‑tuning (webrtcvad)
  - Automatic punctuation & capitalization (punctuators)
  - OCR text extraction from screenshots (pytesseract)
  - Resizable panels (PanedWindow)
  - Compact / floating mode
  - Theme switcher (light/dark)
  - Progress indicator during AI calls
  - Graceful degradation (fallback to Google STT)
  - Auto‑reconnect on audio device change
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import ctypes, threading, sys, io, base64, wave, time, os, json
from datetime import datetime
from PIL import Image
import numpy as np

# ── Windows Stealth ───────────────────────────────────────────────────────────
WDA_EXCLUDEFROMCAPTURE = 0x00000011
def apply_stealth(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
def get_hwnd(widget) -> int:
    hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
    return hwnd if hwnd else widget.winfo_id()

# ── Palette (Dark / Light) ────────────────────────────────────────────────────
DARK = {
    "bg":     "#0d0d1a", "panel":  "#16213e", "input":  "#0f3460",
    "accent": "#e94560", "green":  "#00ff88", "blue":   "#74b9ff",
    "yellow": "#ffd32a", "grey":   "#888888", "white":  "#e0e0e0",
    "red":    "#ff4444", "dim":    "#444466", "snip":   "#00ccff",
    "icon_bg":"#1a1a2e", "progress":"#e94560"
}
LIGHT = {
    "bg":     "#f5f5f5", "panel":  "#e0e0e0", "input":  "#ffffff",
    "accent": "#d63384", "green":  "#198754", "blue":   "#0d6efd",
    "yellow": "#ffc107", "grey":   "#6c757d", "white":  "#212529",
    "red":    "#dc3545", "dim":    "#adb5bd", "snip":   "#0dcaf0",
    "icon_bg":"#f8f9fa", "progress":"#d63384"
}
C = DARK.copy()  # current theme

OPENAI_CHAT_MODEL   = "gpt-4o-mini"
OPENAI_VISION_MODEL = "gpt-4o"
CLAUDE_CHAT_MODEL   = "claude-sonnet-4-6"

SAMPLERATE     = 16000
CHUNK_SEC      = 6
SILENCE_THRESH = 0.005

# Whisper model (lazy load)
_whisper_model = None
def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model

# Punctuator (lazy load)
_punctuator = None
def get_punctuator():
    global _punctuator
    if _punctuator is None:
        from punctuators.models import PunctCapSegModelONNX
        _punctuator = PunctCapSegModelONNX.from_pretrained("pcs_47lang")
    return _punctuator

# ══════════════════════════════════════════════════════════════════════════════
# ✂️  SNIP OVERLAY
# ══════════════════════════════════════════════════════════════════════════════
class SnipOverlay:
    def __init__(self, root, on_done):
        self.on_done = on_done
        self.start_x = self.start_y = 0
        self.cur_rect = None

        self.win = tk.Toplevel(root)
        self.win.attributes("-fullscreen", True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.30)
        self.win.configure(bg="black")
        self.win.overrideredirect(True)
        self.win.update_idletasks()
        apply_stealth(get_hwnd(self.win))

        self.canvas = tk.Canvas(self.win, bg="black", cursor="crosshair", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        sw = self.win.winfo_screenwidth()
        self.canvas.create_rectangle(sw//2-240, 18, sw//2+240, 54,
                                     fill="#111133", outline=C["snip"], width=1)
        self.canvas.create_text(sw//2, 36,
            text="✂️  Drag to select region  ·  Esc = cancel",
            fill="white", font=("Segoe UI", 13, "bold"))

        self.canvas.bind("<ButtonPress-1>",   self._press)
        self.canvas.bind("<B1-Motion>",       self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Escape>", lambda e: self._cancel())

    def _press(self, e):
        self.start_x, self.start_y = e.x, e.y
        if self.cur_rect: self.canvas.delete(self.cur_rect)
        self.cur_rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y,
            outline=C["snip"], width=2, fill="#00aaff", stipple="gray25")

    def _drag(self, e):
        self.canvas.coords(self.cur_rect, self.start_x, self.start_y, e.x, e.y)

    def _release(self, e):
        x1, y1 = min(self.start_x, e.x), min(self.start_y, e.y)
        x2, y2 = max(self.start_x, e.x), max(self.start_y, e.y)
        self.win.destroy()
        if (x2-x1) > 10 and (y2-y1) > 10:
            self.on_done(x1, y1, x2-x1, y2-y1)
        else:
            self.on_done(None, None, None, None)

    def _cancel(self):
        self.win.destroy()
        self.on_done(None, None, None, None)


# ══════════════════════════════════════════════════════════════════════════════
# 🕵️  MAIN APP
# ══════════════════════════════════════════════════════════════════════════════
class StealthAI:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Winconfg")
        try:
            self.root.iconbitmap("C:\\Users\\singh.chandrabhan\\AppData\\Local\\Programs\\Python\\Python313\\Scripts\\settings.ico")
        except:
            pass
        self.root.geometry("540x700")
        self.root.minsize(400, 500)
        self.root.configure(bg=C["bg"])
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.97)

        self.history        = []
        self.api_key        = tk.StringVar(value="Csk-proj-qGAE4REM3Z0VuPxBIaOtsw74yh0pMEOr2xKbS_s3P3eHXTfdE_uLQyZ5b_j8sec3DBpBeeUnzPT3BlbkFJEw59KTF-uTsAiFMp5gZnxar4jjYrZ86SSJBAFAda3QmwjwvuSsJs6xaIzLOaV4jfQ2YdgWBzkA")
        self.provider       = tk.StringVar(value="openai")
        self.visible        = True
        self.meeting_on     = False
        self.compact_mode   = False
        self._alpha_val     = 0.97
        self._toolbar_shown = False
        self._pending_q     = ""
        self._progress      = None

        self._build_ui()
        self._bind_shortcuts()
        self.root.after(200, self._activate_stealth)
        self.root.after(400, self._register_global_hotkeys)

        # Auto‑reconnect monitoring
        self._last_audio_devices = set()
        self._monitor_audio_devices()

    # ── Stealth ───────────────────────────────────────────────────────────────
    def _activate_stealth(self):
        ok = apply_stealth(get_hwnd(self.root))
        self._set_status("🟢 ON" if ok else "🔴 STEALTH FAILED", C["green"] if ok else C["accent"])

    # ── Global hotkeys ────────────────────────────────────────────────────────
    def _register_global_hotkeys(self):
        try:
            import keyboard as kb
            kb.add_hotkey("ctrl+shift+h", self._toggle_visibility)
            kb.add_hotkey("ctrl+shift+s", self._start_snip)
            kb.add_hotkey("ctrl+shift+f", self._start_fullscreen)
            kb.add_hotkey("ctrl+shift+m", self._toggle_meeting)
            self._log("system", "⌨️  Hotkeys: Ctrl+Shift+H (hide)  S (snip)  F (full)  M (meeting)\n\n")
        except Exception as e:
            self._log("error", f"⚠️  Hotkeys failed: {e}\n\n")

    # ── Local shortcuts ───────────────────────────────────────────────────────
    def _bind_shortcuts(self):
        r = self.root
        r.bind("<Control-Return>", lambda e: self._send())
        r.bind("<Control-C>",      lambda e: self._clear_chat())
        r.bind("<Control-I>",      lambda e: self.input_box.focus_set())
        r.bind("<Escape>",         lambda e: self.root.iconify())
        self.input_box.bind("<Control-Return>", lambda e: (self._send(), "break"))

    # ══════════════════════════════════════════════════════════════════════════
    # UI BUILD
    # ══════════════════════════════════════════════════════════════════════════
    def _build_ui(self):
        self._build_header()
        self._build_icon_toolbar()
        self._build_input_area()
        self._build_response_area()
        self._build_transcript_panel()
        self._build_progress_bar()

        self._log("system",
            "StealthAI v8 — Enhanced.\n"
            "✍️  Type or speak (🎤) your question.\n"
            "☰  Toolbar for all features. 🌓 Toggle theme.\n"
        )

    # ── ① Header ──────────────────────────────────────────────────────────────
    def _build_header(self):
        hdr = tk.Frame(self.root, bg=C["panel"], pady=6)
        hdr.pack(fill=tk.X)

        left = tk.Frame(hdr, bg=C["panel"])
        left.pack(side=tk.LEFT, padx=6)

        self.menu_btn = tk.Button(left, text="☰", command=self._toggle_toolbar,
            bg=C["panel"], fg=C["yellow"], bd=0, font=("Segoe UI", 14), cursor="hand2",
            activebackground=C["panel"], activeforeground=C["accent"])
        self.menu_btn.pack(side=tk.LEFT, padx=(0,6))

        tk.Label(left, text="🕵️  Vimlesh AI", font=("Segoe UI", 12, "bold"),
            bg=C["panel"], fg=C["accent"]).pack(side=tk.LEFT)

        right = tk.Frame(hdr, bg=C["panel"])
        right.pack(side=tk.RIGHT, padx=6)

        # Theme toggle button
        self.theme_btn = tk.Button(right, text="🌓", command=self._toggle_theme,
            bg=C["panel"], fg=C["grey"], bd=0, font=("Segoe UI", 11), cursor="hand2",
            activebackground=C["panel"], activeforeground=C["accent"])
        self.theme_btn.pack(side=tk.RIGHT, padx=2)

        for sym, cmd, col in [("✕", self.root.destroy, C["accent"]), ("—", self.root.iconify, C["grey"])]:
            tk.Button(right, text=sym, command=cmd, bg=C["panel"], fg=col, bd=0,
                font=("Segoe UI", 11), cursor="hand2", activebackground=C["panel"],
                activeforeground=C["accent"]).pack(side=tk.RIGHT, padx=2)

        self.status_dot = tk.Label(right, text="⏳", font=("Segoe UI", 9),
            bg=C["panel"], fg=C["grey"])
        self.status_dot.pack(side=tk.RIGHT, padx=6)

    def _set_status(self, msg, color=None):
        color = color or C["grey"]
        dot = "🟢" if "ON" in msg else ("🔴" if "FAIL" in msg else "⏳")
        self.status_dot.config(text=dot, fg=color)
        self.root.title(f"Winconfg  ·  {msg}")

    # ── ② Icon Toolbar ────────────────────────────────────────────────────────
    def _build_icon_toolbar(self):
        self.toolbar = tk.Frame(self.root, bg=C["icon_bg"], pady=4)

        icons = [
            ("🔑", "API Key",       self._panel_api_key),
            ("🤖", "Provider",      self._panel_provider),
            ("🔆", "Opacity",       self._panel_opacity),
            ("✂️", "Snip",          self._start_snip),
            ("📷", "Full Screen",   self._start_fullscreen),
            ("👂", "Meeting",       self._toggle_meeting),
            ("🎤", "Voice Input",   self._start_voice_input),
            ("🙈", "Hide",          self._toggle_visibility),
            ("🗑", "Clear",         self._clear_chat),
            ("⌨️", "Shortcuts",     self._panel_shortcuts),
            ("🪶", "Compact Mode",  self._toggle_compact_mode),
            ("🌓", "Theme",         self._toggle_theme),
        ]

        for emoji, tip, cmd in icons:
            btn = tk.Button(self.toolbar, text=emoji, command=cmd,
                bg=C["icon_bg"], fg=C["white"], bd=0, font=("Segoe UI", 15), cursor="hand2",
                padx=6, pady=2, activebackground=C["panel"], activeforeground=C["yellow"],
                relief=tk.FLAT)
            btn.pack(side=tk.LEFT, expand=True)
            self._add_tooltip(btn, tip)

        self.toolbar_sep = tk.Frame(self.root, bg=C["dim"], height=1)

    def _toggle_toolbar(self):
        if self._toolbar_shown:
            self.toolbar.pack_forget()
            self.toolbar_sep.pack_forget()
            self._toolbar_shown = False
            self.menu_btn.config(fg=C["yellow"])
        else:
            self.toolbar.pack(fill=tk.X, after=self._get_header_frame())
            self.toolbar_sep.pack(fill=tk.X, after=self.toolbar)
            self._toolbar_shown = True
            self.menu_btn.config(fg=C["green"])

    def _get_header_frame(self):
        return self.root.pack_slaves()[0]

    # ── ③ INPUT ON TOP ────────────────────────────────────────────────────────
    def _build_input_area(self):
        lbl_row = tk.Frame(self.root, bg=C["panel"], pady=3)
        lbl_row.pack(fill=tk.X, padx=8, pady=(6,0))
        tk.Label(lbl_row, text="✍️  Your question:", font=("Segoe UI", 9, "bold"),
            bg=C["panel"], fg=C["white"]).pack(side=tk.LEFT, padx=6)
        tk.Label(lbl_row, text="Ctrl+Enter = send  ·  Shift+Enter = newline",
            font=("Segoe UI", 7), bg=C["panel"], fg=C["grey"]).pack(side=tk.RIGHT, padx=6)

        inp = tk.Frame(self.root, bg=C["panel"])
        inp.pack(fill=tk.X, padx=8, pady=(0,0))

        self.input_box = tk.Text(inp, height=3, font=("Segoe UI", 11),
            bg=C["input"], fg=C["white"], insertbackground=C["white"],
            relief=tk.FLAT, bd=6, wrap=tk.WORD)
        self.input_box.pack(fill=tk.X)
        self.input_box.bind("<Escape>", lambda e: self.root.iconify())

        self.send_btn = tk.Button(inp, text="  ➤  Send Message  [Ctrl+Enter]",
            command=self._send, bg=C["accent"], fg="white", bd=0,
            font=("Segoe UI", 10, "bold"), pady=6, cursor="hand2",
            activebackground="#c73652")
        self.send_btn.pack(fill=tk.X, pady=(4,6))

        tk.Frame(self.root, bg=C["dim"], height=1).pack(fill=tk.X, padx=8)

    # ── ④ AI RESPONSE + TRANSCRIPT (resizable panes) ──────────────────────────
    def _build_response_area(self):
        top = tk.Frame(self.root, bg=C["bg"])
        top.pack(fill=tk.X, padx=8, pady=(3,0))
        tk.Label(top, text="🤖  AI Response", font=("Segoe UI", 8, "bold"),
            bg=C["bg"], fg=C["dim"]).pack(side=tk.LEFT)
        self.mtg_indicator = tk.Label(top, text="", font=("Segoe UI", 7),
            bg=C["bg"], fg=C["green"])
        self.mtg_indicator.pack(side=tk.RIGHT)

        # Create a PanedWindow for resizable chat/transcript split
        self.main_pane = tk.PanedWindow(self.root, orient=tk.VERTICAL,
            bg=C["bg"], sashrelief=tk.RAISED, sashwidth=4)
        self.main_pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2,8))

        # Chat area (top)
        self.chat_frame = tk.Frame(self.main_pane, bg=C["bg"])
        self.main_pane.add(self.chat_frame, stretch="always")
        self.chat = scrolledtext.ScrolledText(self.chat_frame, wrap=tk.WORD,
            state=tk.DISABLED, font=("Segoe UI", 11), bg=C["bg"], fg=C["white"],
            relief=tk.FLAT, padx=10, pady=6, selectbackground=C["input"])
        self.chat.pack(fill=tk.BOTH, expand=True)

        self.chat.tag_config("user",   foreground=C["blue"],   font=("Segoe UI", 11, "bold"))
        self.chat.tag_config("ai",     foreground=C["green"],  font=("Segoe UI", 11))
        self.chat.tag_config("label",  foreground=C["accent"], font=("Segoe UI", 10, "bold"))
        self.chat.tag_config("system", foreground=C["dim"],    font=("Segoe UI", 9,  "italic"))
        self.chat.tag_config("error",  foreground=C["red"],    font=("Segoe UI", 10))
        self.chat.tag_config("heard",  foreground=C["yellow"], font=("Segoe UI", 10, "italic"))

    def _build_transcript_panel(self):
        self.transcript_frame = tk.Frame(self.main_pane, bg=C["panel"])
        self.main_pane.add(self.transcript_frame, stretch="never", height=120)

        header = tk.Frame(self.transcript_frame, bg=C["panel"])
        header.pack(fill=tk.X, pady=(2,0))
        tk.Label(header, text="📝 Meeting Transcript", font=("Segoe UI", 8, "bold"),
            bg=C["panel"], fg=C["grey"]).pack(side=tk.LEFT, padx=2)

        self.transcript = scrolledtext.ScrolledText(self.transcript_frame,
            wrap=tk.WORD, state=tk.DISABLED, height=6, font=("Consolas", 9),
            bg=C["input"], fg="#aaccff", relief=tk.FLAT, padx=6, pady=4,
            selectbackground=C["accent"])
        self.transcript.pack(fill=tk.BOTH, expand=True)
        self.transcript.tag_config("transcript", foreground="#aaccff", font=("Consolas", 9))

    def _build_progress_bar(self):
        self.progress = ttk.Progressbar(self.root, mode='indeterminate',
            style="red.Horizontal.TProgressbar")
        # Custom style for progress bar
        style = ttk.Style()
        style.configure("red.Horizontal.TProgressbar", background=C["progress"])

    # ── Theme switching ───────────────────────────────────────────────────────
    def _toggle_theme(self):
        global C
        if C == DARK:
            C.update(LIGHT)
        else:
            C.update(DARK)
        self._apply_theme()

    def _apply_theme(self):
        """Reconfigure all widgets with new colors."""
        self.root.configure(bg=C["bg"])
        # Recursively update colors (simplified: we'll just restart UI? Better to reconfigure)
        # For brevity, we'll rebuild the UI.
        for w in self.root.winfo_children():
            w.destroy()
        self._build_ui()
        self._apply_stealth_after_rebuild()

    def _apply_stealth_after_rebuild(self):
        self.root.after(100, self._activate_stealth)

    # ── Compact mode ──────────────────────────────────────────────────────────
    def _toggle_compact_mode(self):
        self.compact_mode = not self.compact_mode
        if self.compact_mode:
            self.root.geometry("300x200")
            self.root.attributes("-topmost", True)
            self.input_box.config(height=1)
            self.chat_frame.pack_forget()
            self.transcript_frame.pack_forget()
            self.main_pane.pack_forget()
        else:
            self.root.geometry("540x700")
            self.input_box.config(height=3)
            self.main_pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2,8))
            self.chat_frame.pack(fill=tk.BOTH, expand=True)
            self.transcript_frame.pack(fill=tk.BOTH, expand=True)

    # ══════════════════════════════════════════════════════════════════════════
    # PANELS (API key, provider, etc.) – unchanged but colors updated via C
    # ══════════════════════════════════════════════════════════════════════════
    def _make_panel(self, title, width=300, height=120):
        p = tk.Toplevel(self.root)
        p.title(title)
        p.geometry(f"{width}x{height}")
        p.configure(bg=C["panel"])
        p.attributes("-topmost", True)
        p.after(150, lambda: apply_stealth(get_hwnd(p)))
        return p

    def _panel_api_key(self):
        p = self._make_panel("🔑 API Key", 380, 100)
        tk.Label(p, text="API Key:", font=("Segoe UI", 9),
                 bg=C["panel"], fg=C["grey"]).pack(anchor="w", padx=10, pady=(10,2))
        row = tk.Frame(p, bg=C["panel"])
        row.pack(fill=tk.X, padx=10)
        e = tk.Entry(row, textvariable=self.api_key, show="●",
                     font=("Consolas", 9), bg=C["input"], fg=C["white"],
                     insertbackground=C["white"], relief=tk.FLAT, bd=5)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True)
        show_var = tk.BooleanVar(value=False)
        def toggle():
            e.config(show="" if show_var.get() else "●")
        tk.Checkbutton(row, text="👁", variable=show_var, command=toggle,
                       bg=C["panel"], fg=C["grey"], selectcolor=C["panel"],
                       activebackground=C["panel"], bd=0).pack(side=tk.RIGHT, padx=4)
        hint = "👉  OpenAI: platform.openai.com/api-keys" if self.provider.get()=="openai" else "👉  Claude: console.anthropic.com"
        tk.Label(p, text=hint, font=("Segoe UI", 7), bg=C["panel"], fg=C["dim"]).pack(anchor="w", padx=10, pady=2)

    def _panel_provider(self):
        p = self._make_panel("🤖 Provider", 280, 110)
        tk.Label(p, text="Select AI Provider:", font=("Segoe UI", 9), bg=C["panel"], fg=C["grey"]
                 ).pack(anchor="w", padx=10, pady=(10,4))
        tk.Radiobutton(p, text="ChatGPT  ✅  (gpt-4o-mini / gpt-4o)",
                       variable=self.provider, value="openai", bg=C["panel"], fg=C["blue"],
                       selectcolor=C["panel"], activebackground=C["panel"],
                       font=("Segoe UI", 9, "bold"), cursor="hand2").pack(anchor="w", padx=20)
        tk.Radiobutton(p, text="Claude  🔒  (future — needs Anthropic key)",
                       variable=self.provider, value="claude", bg=C["panel"], fg=C["dim"],
                       selectcolor=C["panel"], activebackground=C["panel"],
                       font=("Segoe UI", 9), cursor="hand2").pack(anchor="w", padx=20)

    def _panel_opacity(self):
        p = self._make_panel("🔆 Window Opacity", 300, 90)
        tk.Label(p, text="Drag to adjust transparency:", font=("Segoe UI", 8),
                 bg=C["panel"], fg=C["grey"]).pack(anchor="w", padx=10, pady=(8,2))
        row = tk.Frame(p, bg=C["panel"])
        row.pack(fill=tk.X, padx=10)
        self.alpha_var = tk.IntVar(value=int(self._alpha_val*100))
        lbl = tk.Label(row, text=f"{int(self._alpha_val*100)}%",
                       font=("Consolas", 9), bg=C["panel"], fg=C["blue"], width=4)
        lbl.pack(side=tk.RIGHT)
        def on_change(val):
            pct = int(val)
            self._alpha_val = pct/100.0
            self.root.attributes("-alpha", self._alpha_val)
            lbl.config(text=f"{pct}%")
        tk.Scale(row, from_=20, to=100, variable=self.alpha_var,
                 orient=tk.HORIZONTAL, command=on_change, bg=C["panel"], fg=C["grey"],
                 troughcolor=C["input"], highlightthickness=0, bd=0, showvalue=False,
                 sliderrelief=tk.FLAT, sliderlength=14).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,6))

    def _panel_shortcuts(self):
        p = self._make_panel("⌨️ Shortcuts", 340, 220)
        shortcuts = [
            ("Ctrl+Enter",   "Send message"), ("Ctrl+Shift+C", "Clear chat"),
            ("Ctrl+Shift+I", "Focus input"), ("Escape", "Minimise window"),
            ("Ctrl+Shift+H", "Hide/Show  🌐"), ("Ctrl+Shift+S", "Snip region  🌐"),
            ("Ctrl+Shift+F", "Full screen  🌐"), ("Ctrl+Shift+M", "Meeting ON/OFF  🌐"),
        ]
        tk.Label(p, text="⌨️  All Shortcuts  (🌐 = works when hidden)",
                 font=("Segoe UI", 8, "bold"), bg=C["panel"], fg=C["grey"]
                 ).pack(anchor="w", padx=10, pady=(8,4))
        grid = tk.Frame(p, bg=C["panel"])
        grid.pack(fill=tk.X, padx=10)
        for i, (key, desc) in enumerate(shortcuts):
            tk.Label(grid, text=key, font=("Consolas", 8, "bold"),
                     bg=C["panel"], fg=C["blue"], width=16, anchor="w"
                     ).grid(row=i, column=0, sticky="w", pady=1)
            tk.Label(grid, text=desc, font=("Segoe UI", 8),
                     bg=C["panel"], fg=C["white"], anchor="w"
                     ).grid(row=i, column=1, sticky="w", padx=6)

    # ── Tooltip helper ────────────────────────────────────────────────────────
    def _add_tooltip(self, widget, text):
        tip = None
        def enter(e):
            nonlocal tip
            tip = tk.Toplevel(self.root)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            tip.configure(bg=C["panel"])
            tk.Label(tip, text=text, font=("Segoe UI", 8),
                     bg=C["panel"], fg=C["white"], padx=6, pady=2).pack()
            tip.geometry(f"+{e.x_root+12}+{e.y_root+18}")
        def leave(e):
            nonlocal tip
            if tip: tip.destroy(); tip = None
        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _log(self, tag, text):
        self.chat.config(state=tk.NORMAL)
        self.chat.insert(tk.END, text, tag)
        self.chat.see(tk.END)
        self.chat.config(state=tk.DISABLED)

    def _stream_char(self, ch):
        self.chat.config(state=tk.NORMAL)
        self.chat.insert(tk.END, ch, "ai")
        self.chat.see(tk.END)
        self.chat.config(state=tk.DISABLED)

    def _clear_chat(self):
        self.history.clear()
        self.chat.config(state=tk.NORMAL)
        self.chat.delete("1.0", tk.END)
        self.chat.config(state=tk.DISABLED)
        self.transcript.config(state=tk.NORMAL)
        self.transcript.delete("1.0", tk.END)
        self.transcript.config(state=tk.DISABLED)
        self._log("system", "Chat cleared.\n\n")

    def _lock_ui(self, locked: bool):
        state = tk.DISABLED if locked else tk.NORMAL
        for w in (self.send_btn, self.input_box):
            w.config(state=state)
        if locked:
            self.progress.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=2)
            self.progress.start(10)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def _check_key(self) -> bool:
        key = self.api_key.get().strip()
        if not key or key == "sk-...":
            self._log("error", "⚠️  Enter your API key — click 🔑 in the toolbar (☰ to open).\n\n")
            return False
        return True

    def _toggle_visibility(self):
        if self.visible:
            self.root.withdraw()
            self.visible = False
        else:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.attributes("-alpha", self._alpha_val)
            self.visible = True

    def _get_question(self) -> str:
        text = self.input_box.get("1.0", tk.END).strip()
        if text: self.input_box.delete("1.0", tk.END)
        return text or "Analyse this screenshot and describe what you see."

    # ── Send text ─────────────────────────────────────────────────────────────
    def _send(self):
        text = self.input_box.get("1.0", tk.END).strip()
        if not text or not self._check_key(): return
        self.input_box.delete("1.0", tk.END)
        self._log("user", f"\nYou:  {text}\n")
        self.history.append({"role": "user", "content": text})
        self._lock_ui(True)
        threading.Thread(target=self._call_text, daemon=True).start()

    def _call_text(self):
        try:
            if self.provider.get() == "openai": self._openai_text()
            else: self._claude_text()
        except Exception as e:
            self.root.after(0, self._log, "error", f"⚠️  {e}\n\n")
        finally:
            self.root.after(0, self._lock_ui, False)

    # ══════════════════════════════════════════════════════════════════════════
    # 📷 FULL SCREEN (with OCR)
    # ══════════════════════════════════════════════════════════════════════════
    def _start_fullscreen(self):
        if not self._check_key(): return
        question = self._get_question()
        self._log("system", "📷  Capturing full screen in 0.5 s…\n")
        self.root.withdraw()
        self.root.after(500, lambda: self._do_fullscreen(question))

    def _do_fullscreen(self, question):
        try:
            import mss
        except ImportError:
            self.root.deiconify()
            self._log("error", "⚠️  pip install mss Pillow\n\n")
            return
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self._alpha_val)
        self.visible = True

        # OCR
        ocr_text = self._extract_ocr(img)
        if ocr_text:
            question = f"OCR text from screen:\n{ocr_text}\n\nQuestion: {question}"

        w, h = img.size
        if w > 1280: img = img.resize((1280, int(h*1280/w)))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        self._log("user", f"📷  [Full Screen]  {question[:80]}...\n")
        self._lock_ui(True)
        threading.Thread(target=self._run_vision, args=(self.provider.get(), b64, question), daemon=True).start()

    # ══════════════════════════════════════════════════════════════════════════
    # ✂️ SNIP (with OCR)
    # ══════════════════════════════════════════════════════════════════════════
    def _start_snip(self):
        if not self._check_key(): return
        self._pending_q = self._get_question()
        self._log("system", "✂️  Draw your selection…\n")
        self.root.withdraw()
        self.root.after(300, lambda: SnipOverlay(self.root, self._on_snip_done))

    def _on_snip_done(self, x, y, w, h):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self._alpha_val)
        self.visible = True
        if x is None: self._log("system", "✂️  Snip cancelled.\n\n"); return
        try:
            import mss
        except ImportError:
            self._log("error", "⚠️  pip install mss Pillow\n\n"); return
        with mss.mss() as sct:
            raw = sct.grab({"left": x, "top": y, "width": w, "height": h})
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        ocr_text = self._extract_ocr(img)
        question = self._pending_q
        if ocr_text:
            question = f"OCR text from snip:\n{ocr_text}\n\nQuestion: {question}"
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        self._log("user", f"✂️  [Snip {w}×{h}px]  {self._pending_q}\n")
        self._lock_ui(True)
        threading.Thread(target=self._run_vision, args=(self.provider.get(), b64, question), daemon=True).start()

    def _extract_ocr(self, img: Image) -> str:
        try:
            import pytesseract
            return pytesseract.image_to_string(img).strip()
        except Exception:
            return ""

    def _run_vision(self, provider, b64, question):
        try:
            if provider == "openai": self._openai_vision(b64, question)
            else: self._claude_vision(b64, question)
        except Exception as e:
            self.root.after(0, self._log, "error", f"⚠️  {e}\n\n")
        finally:
            self.root.after(0, self._lock_ui, False)

    # ══════════════════════════════════════════════════════════════════════════
    # ██ OPENAI
    # ══════════════════════════════════════════════════════════════════════════
    def _openai_text(self, max_tokens=1024, system_override=None):
        import openai
        client = openai.OpenAI(api_key=self.api_key.get().strip())
        system = system_override or "You are a concise AI assistant."
        msgs = [{"role": "system", "content": system}] + self.history
        self.root.after(0, self._log, "label", "\nChatGPT:  ")
        full = ""
        for chunk in client.chat.completions.create(model=OPENAI_CHAT_MODEL, messages=msgs, stream=True, max_tokens=max_tokens):
            ch = chunk.choices[0].delta.content or ""
            if ch: full += ch; self.root.after(0, self._stream_char, ch)
        self.root.after(0, self._log, "ai", "\n\n")
        self.history.append({"role": "assistant", "content": full})

    def _openai_vision(self, b64, question):
        import openai
        client = openai.OpenAI(api_key=self.api_key.get().strip())
        self.root.after(0, self._log, "label", "\nChatGPT (Vision):  ")
        resp = client.chat.completions.create(model=OPENAI_VISION_MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
                {"type": "text", "text": question}
            ]}])
        full = resp.choices[0].message.content or ""
        for ch in full: self.root.after(0, self._stream_char, ch)
        self.root.after(0, self._log, "ai", "\n\n")
        self.history.append({"role": "user", "content": f"[Screenshot] {question}"})
        self.history.append({"role": "assistant", "content": full})

    # ══════════════════════════════════════════════════════════════════════════
    # ██ CLAUDE
    # ══════════════════════════════════════════════════════════════════════════
    def _claude_text(self, max_tokens=1024, system_override=None):
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key.get().strip())
        system = system_override or "You are a concise AI assistant."
        self.root.after(0, self._log, "label", "\nClaude:  ")
        full = ""
        with client.messages.stream(model=CLAUDE_CHAT_MODEL, max_tokens=max_tokens,
                                    system=system, messages=self.history) as stream:
            for ch in stream.text_stream: full += ch; self.root.after(0, self._stream_char, ch)
        self.root.after(0, self._log, "ai", "\n\n")
        self.history.append({"role": "assistant", "content": full})

    def _claude_vision(self, b64, question):
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key.get().strip())
        self.root.after(0, self._log, "label", "\nClaude (Vision):  ")
        full = ""
        with client.messages.stream(model=CLAUDE_CHAT_MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": question}
            ]}]) as stream:
            for ch in stream.text_stream: full += ch; self.root.after(0, self._stream_char, ch)
        self.root.after(0, self._log, "ai", "\n\n")
        self.history.append({"role": "user", "content": f"[Screenshot] {question}"})
        self.history.append({"role": "assistant", "content": full})

    # ══════════════════════════════════════════════════════════════════════════
    # 🎤 VOICE INPUT (Whisper + noise reduction + punctuation)
    # ══════════════════════════════════════════════════════════════════════════
    def _start_voice_input(self):
        if not self._check_key(): return
        self._log("system", "🎤  Recording… (stops after silence)\n")
        self._set_status("🎤 Recording…", C["blue"])
        self._lock_ui(True)
        threading.Thread(target=self._do_voice_input_dynamic, daemon=True).start()

    def _do_voice_input_dynamic(self):
        try:
            import soundcard as sc
        except ImportError:
            self.root.after(0, self._log, "error", "⚠️  pip install soundcard\n\n")
            self.root.after(0, self._set_status, "Ready"); self.root.after(0, self._lock_ui, False); return
        try:
            mic = sc.default_microphone()
        except Exception as e:
            self.root.after(0, self._log, "error", f"⚠️  Mic error: {e}\n"); self.root.after(0, self._set_status, "Ready"); self.root.after(0, self._lock_ui, False); return

        # VAD with webrtcvad
        try:
            from webrtcvad import Vad
            vad = Vad(2)  # aggressiveness 0-3
        except:
            vad = None

        SILENCE_THRESH = 0.008
        SILENCE_DUR = 1.5
        MAX_SEC = 30
        CHUNK = 480  # 30ms at 16kHz for webrtcvad

        frames = []
        silent_chunks = 0
        chunk_count = 0

        try:
            with mic.recorder(samplerate=SAMPLERATE, channels=1) as rec:
                while True:
                    data = rec.record(numframes=CHUNK)
                    mono = data[:,0] if data.ndim>1 else data
                    frames.append(mono)
                    rms = float(np.sqrt(np.mean(mono**2)))
                    is_speech = rms > SILENCE_THRESH
                    if vad is not None:
                        pcm = (np.clip(mono, -1,1)*32767).astype(np.int16)
                        try:
                            is_speech = vad.is_speech(pcm.tobytes(), SAMPLERATE)
                        except:
                            pass
                    if is_speech: silent_chunks = 0
                    else: silent_chunks += 1
                    if (chunk_count*CHUNK)/SAMPLERATE > MAX_SEC: break
                    if silent_chunks * CHUNK / SAMPLERATE >= SILENCE_DUR: break
                    chunk_count += 1
        except Exception as e:
            self.root.after(0, self._log, "error", f"⚠️  Record error: {e}\n")
            self.root.after(0, self._set_status, "Ready"); self.root.after(0, self._lock_ui, False); return

        if not frames:
            self.root.after(0, self._log, "system", "🎤  No audio.\n\n")
            self.root.after(0, self._set_status, "Ready"); self.root.after(0, self._lock_ui, False); return

        audio = np.concatenate(frames)
        # Noise reduction
        try:
            import noisereduce as nr
            audio = nr.reduce_noise(y=audio, sr=SAMPLERATE)
        except:
            pass

        # Transcribe with Whisper
        text = self._transcribe_whisper(audio)
        if not text:
            # Fallback to Google
            try:
                import speech_recognition as sr
                pcm = (np.clip(audio, -1,1)*32767).astype(np.int16)
                wav_buf = io.BytesIO()
                with wave.open(wav_buf, "wb") as wf:
                    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SAMPLERATE); wf.writeframes(pcm.tobytes())
                wav_buf.seek(0)
                r = sr.Recognizer()
                text = r.recognize_google(sr.AudioData(wav_buf.read(), SAMPLERATE, 2))
            except:
                text = ""

        if text:
            # Punctuate
            try:
                punct = get_punctuator()
                text = punct.infer([text])[0]
            except:
                pass
            self.root.after(0, self._log, "system", f'🎤  Recognised: "{text}"\n')
            self.root.after(0, lambda: self.input_box.delete("1.0", tk.END))
            self.root.after(0, lambda: self.input_box.insert("1.0", text))
            self.root.after(0, self._send)
        else:
            self.root.after(0, self._log, "system", "🎤  Could not understand.\n\n")
        self.root.after(0, self._set_status, "Ready")
        self.root.after(0, self._lock_ui, False)

    def _transcribe_whisper(self, audio: np.ndarray) -> str:
        try:
            model = get_whisper_model()
            segments, _ = model.transcribe(audio.astype(np.float32), language="en", beam_size=5)
            return " ".join([seg.text for seg in segments])
        except Exception as e:
            print(f"Whisper error: {e}")
            return ""

    # ══════════════════════════════════════════════════════════════════════════
    # ██ MEETING LISTENER (Whisper + VAD + noise reduction + punctuation)
    # ══════════════════════════════════════════════════════════════════════════
    def _toggle_meeting(self):
        if not self.visible: self._toggle_visibility()
        if self.meeting_on:
            self.meeting_on = False
            self.mtg_indicator.config(text="")
            self._log("system", "👂  Meeting listener stopped.\n\n")
        else:
            if not self._check_key(): return
            self.meeting_on = True
            self.mtg_indicator.config(text="🔴 LIVE")
            self._log("system", "👂  Meeting listener ON (Whisper+VAD).\n")
            self.transcript.config(state=tk.NORMAL)
            self.transcript.delete("1.0", tk.END)
            self.transcript.config(state=tk.DISABLED)
            threading.Thread(target=self._meeting_loop_vad, daemon=True).start()

    def _meeting_loop_vad(self):
        try:
            import soundcard as sc
            from webrtcvad import Vad
            vad = Vad(2)
        except Exception as e:
            self.root.after(0, self._log, "error", f"⚠️  VAD missing: {e}\n")
            self.root.after(0, self._toggle_meeting); return

        try:
            spk = sc.default_speaker()
            loop = sc.get_microphone(id=str(spk.name), include_loopback=True)
        except Exception as e:
            self.root.after(0, self._log, "error", f"⚠️  Loopback failed: {e}\n")
            self.root.after(0, self._toggle_meeting); return

        self.root.after(0, self._log, "system", f"🎧  Loopback: {spk.name}\n\n")
        CHUNK = 480  # 30ms
        speech_frames = []
        silence_frames = []
        is_speaking = False
        silence_dur = 0.0
        speech_dur = 0.0
        MIN_UTTERANCE = 1.0
        MAX_UTTERANCE = 15.0
        PAUSE_THRESH = 1.2

        while self.meeting_on:
            try:
                with loop.recorder(samplerate=SAMPLERATE, channels=1) as mic:
                    data = mic.record(numframes=CHUNK)
                mono = data[:,0] if data.ndim>1 else data
                pcm = (np.clip(mono, -1,1)*32767).astype(np.int16)
                is_speech = vad.is_speech(pcm.tobytes(), SAMPLERATE)

                if is_speech:
                    if not is_speaking:
                        is_speaking = True
                        if silence_frames:
                            speech_frames.extend(silence_frames[-int(0.3/0.03):])
                            silence_frames.clear()
                        self.root.after(0, self.mtg_indicator.config, {"text": "🔴 SPEAKING"})
                    speech_frames.append(mono)
                    speech_dur += 0.03
                    silence_dur = 0.0
                else:
                    if is_speaking:
                        silence_frames.append(mono)
                        silence_dur += 0.03
                        if silence_dur >= PAUSE_THRESH:
                            if speech_dur >= MIN_UTTERANCE:
                                self._process_utterance_vad(speech_frames)
                            speech_frames.clear(); silence_frames.clear()
                            is_speaking = False; speech_dur=0.0; silence_dur=0.0
                            self.root.after(0, self.mtg_indicator.config, {"text": "🔴 LIVE"})
                    else:
                        silence_frames.append(mono)
                        if len(silence_frames) > int(0.3/0.03): silence_frames.pop(0)
                if is_speaking and speech_dur >= MAX_UTTERANCE:
                    self._process_utterance_vad(speech_frames)
                    speech_frames.clear(); silence_frames.clear()
                    is_speaking = False; speech_dur=0.0; silence_dur=0.0
                    self.root.after(0, self.mtg_indicator.config, {"text": "🔴 LIVE"})
            except Exception as e:
                self.root.after(0, self._log, "error", f"⚠️  Meeting error: {e}\n")
                time.sleep(0.5)
                # Auto‑reconnect on device change
                self._check_audio_device_change()

    def _process_utterance_vad(self, frames):
        if not frames: return
        audio = np.concatenate(frames)
        # Noise reduction
        try:
            import noisereduce as nr
            audio = nr.reduce_noise(y=audio, sr=SAMPLERATE)
        except: pass
        text = self._transcribe_whisper(audio)
        if not text:
            # Fallback Google
            try:
                import speech_recognition as sr
                pcm = (np.clip(audio, -1,1)*32767).astype(np.int16)
                wav_buf = io.BytesIO()
                with wave.open(wav_buf, "wb") as wf:
                    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SAMPLERATE); wf.writeframes(pcm.tobytes())
                wav_buf.seek(0)
                r = sr.Recognizer()
                text = r.recognize_google(sr.AudioData(wav_buf.read(), SAMPLERATE, 2))
            except: return
        if text:
            try:
                punct = get_punctuator()
                text = punct.infer([text])[0]
            except: pass
            self.root.after(0, self._log_transcript, text)
            self.root.after(0, self._log, "heard", f'🎧  "{text}"\n')
            prompt = f'Someone said:\n"{text}"\n\nShort helpful reply (2-3 sentences).'
            self.history.append({"role": "user", "content": prompt})
            sys_msg = "Be a concise meeting assistant."
            try:
                if self.provider.get()=="openai": self._openai_text(max_tokens=250, system_override=sys_msg)
                else: self._claude_text(max_tokens=250, system_override=sys_msg)
            except Exception as e:
                self.root.after(0, self._log, "error", f"⚠️  AI: {e}\n\n")

    def _log_transcript(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {text}\n"
        self.transcript.config(state=tk.NORMAL)
        self.transcript.insert(tk.END, line, "transcript")
        self.transcript.see(tk.END)
        self.transcript.config(state=tk.DISABLED)

    # ── Audio device monitoring for auto‑reconnect ────────────────────────────
    def _monitor_audio_devices(self):
        def check():
            if self.meeting_on:
                self._check_audio_device_change()
            self.root.after(5000, check)
        self.root.after(5000, check)

    def _check_audio_device_change(self):
        try:
            import soundcard as sc
            current = set([sc.default_speaker().name])
            if current != self._last_audio_devices:
                self._last_audio_devices = current
                if self.meeting_on:
                    self._log("system", "🔄 Audio device changed, restarting listener...\n")
                    self.meeting_on = False
                    self.root.after(500, self._toggle_meeting)
        except: pass

    # ── Run ───────────────────────────────────────────────────────────────────
    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if sys.platform != "win32":
        print("⚠️  Stealth mode is Windows-only.")
    StealthAI().run()

