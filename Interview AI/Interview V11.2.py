"""
StealthAI v11 — Deepgram Live STT + Whisper Fallback
=====================================================
NEW in v11:
  - Deepgram live streaming STT — words appear on screen AS you speak
  - At 3 s silence → text already ready → instant send to AI (saves 4-6 s)
  - 🎙️ toolbar button toggles STT engine: Deepgram ↔ Whisper
  - Deepgram key field added to existing 🔑 API Key panel
  - Auto-fallback: Deepgram fails → silently switches to Whisper
    shows: ⚠️ Deepgram unavailable — switched to Whisper
  - Auto-recover: Deepgram reconnects → shows: ✅ Deepgram reconnected
  - Live words stream into existing transcript panel (bottom)
  - Both 🎤 Voice and 👂 Meeting use same Deepgram/Whisper logic

Carried from v10:
  - Voice Input toggle ON/OFF (mic source)
  - Meeting toggle ON/OFF (speaker loopback source)
  - 3 s silence accumulation logic
  - COM init/uninit (fixes 0x800401f0)
  - Bluetooth auto-detect + reconnect
  - Audio device selector panel
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import ctypes, threading, sys, io, base64, wave, time, json, queue
from datetime import datetime
from PIL import Image
import numpy as np

# ── COM helpers ───────────────────────────────────────────────────────────────
def com_init():
    try:
        return ctypes.windll.ole32.CoInitializeEx(None, 2) >= 0
    except Exception:
        return False

def com_uninit():
    try:
        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass

# ── Windows Stealth ───────────────────────────────────────────────────────────
WDA_EXCLUDEFROMCAPTURE = 0x00000011

def apply_stealth(hwnd):
    return bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))

def get_hwnd(widget):
    hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
    return hwnd if hwnd else widget.winfo_id()

# ── Palettes ──────────────────────────────────────────────────────────────────
DARK = {
    "bg": "#0d0d1a", "panel": "#16213e", "input": "#0f3460",
    "accent": "#e94560", "green": "#00ff88", "blue": "#74b9ff",
    "yellow": "#ffd32a", "grey": "#888888", "white": "#e0e0e0",
    "red": "#ff4444", "dim": "#444466", "snip": "#00ccff",
    "icon_bg": "#1a1a2e", "progress": "#e94560"
}
LIGHT = {
    "bg": "#f5f5f5", "panel": "#e0e0e0", "input": "#ffffff",
    "accent": "#d63384", "green": "#198754", "blue": "#0d6efd",
    "yellow": "#ffc107", "grey": "#6c757d", "white": "#212529",
    "red": "#dc3545", "dim": "#adb5bd", "snip": "#0dcaf0",
    "icon_bg": "#f8f9fa", "progress": "#d63384"
}
C = DARK.copy()

OPENAI_CHAT_MODEL   = "gpt-4o-mini"
OPENAI_VISION_MODEL = "gpt-4o"
CLAUDE_CHAT_MODEL   = "claude-sonnet-4-6"

SAMPLERATE       = 16000
CHUNK_FRAMES     = 480          # 30 ms @ 16 kHz — required by webrtcvad
CHUNK_SEC        = CHUNK_FRAMES / SAMPLERATE
SEND_SILENCE_SEC = 3.0          # silence duration before sending to AI
MIN_SPEECH_SEC   = 0.1          # ignore pure noise bursts
MAX_SPEECH_SEC   = 60.0         # safety cap
SILENCE_THRESH   = 0.005

# ── Lazy models ───────────────────────────────────────────────────────────────
_whisper_model = None
def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model

_punctuator = None
def get_punctuator():
    global _punctuator
    if _punctuator is None:
        from punctuators.models import PunctCapSegModelONNX
        _punctuator = PunctCapSegModelONNX.from_pretrained("pcs_47lang")
    return _punctuator

# ── Audio helpers ─────────────────────────────────────────────────────────────
def get_best_audio_source():
    try:
        import soundcard as sc
        try:
            spk  = sc.default_speaker()
            loop = sc.get_microphone(id=str(spk.name), include_loopback=True)
            return loop, f"🔊 {spk.name}", True
        except Exception:
            pass
        bt_kw = ["bluetooth", "bt ", "airpod", "headset", "wireless", "a2dp"]
        for spk in sc.all_speakers():
            if any(k in spk.name.lower() for k in bt_kw):
                try:
                    loop = sc.get_microphone(id=str(spk.name), include_loopback=True)
                    return loop, f"🔵 BT: {spk.name}", True
                except Exception:
                    pass
        mic = sc.default_microphone()
        return mic, f"🎤 {mic.name}", False
    except Exception as e:
        return None, str(e), False


# ══════════════════════════════════════════════════════════════════════════════
# ✂️  SNIP OVERLAY
# ══════════════════════════════════════════════════════════════════════════════
class SnipOverlay:
    def __init__(self, root, on_done):
        self.on_done  = on_done
        self.start_x  = self.start_y = 0
        self.cur_rect = None
        self.win = tk.Toplevel(root)
        self.win.attributes("-fullscreen", True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.30)
        self.win.configure(bg="black")
        self.win.overrideredirect(True)
        self.win.update_idletasks()
        apply_stealth(get_hwnd(self.win))
        self.canvas = tk.Canvas(self.win, bg="black", cursor="crosshair",
                                highlightthickness=0)
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
        if self.cur_rect:
            self.canvas.delete(self.cur_rect)
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
            self.root.iconbitmap("F:\\AI\\Intervie AI\\settings.ico")
        except Exception:
            pass
        self.root.geometry("540x700")
        self.root.minsize(400, 500)
        self.root.configure(bg=C["bg"])
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.97)

        self.history           = []
        self.api_key           = tk.StringVar(value="Csk-proj-qGAE4REM3Z0VuPxBIaOtsw74yh0pMEOr2xKbS_s3P3eHXTfdE_uLQyZ5b_j8sec3DBpBeeUnzPT3BlbkFJEw59KTF-uTsAiFMp5gZnxar4jjYrZ86SSJBAFAda3QmwjwvuSsJs6xaIzLOaV4jfQ2YdgWBzkA")
        self.deepgram_key      = tk.StringVar(value="C61c069dda9630a1fb171c26d870ea65a449f7316")        # NEW
        self.stt_engine        = tk.StringVar(value="deepgram") # NEW: deepgram | whisper
        self.provider          = tk.StringVar(value="openai")
        self.visible           = True
        self.compact_mode      = False
        self._alpha_val        = 0.97
        self._toolbar_shown    = False
        self._pending_q        = ""
        self._selected_device  = None
        self._last_audio_state = ""

        # Listener states
        self.meeting_on = False
        self.voice_on   = False

        # Font size for AI response area
        self._chat_font_size = 11   # default, range 7-16

        # Live transcript line tracking (for Deepgram live updates)
        self._live_line_start = None   # index of start of current live line

        self._build_ui()
        self._bind_shortcuts()
        self.root.after(200, self._activate_stealth)
        self.root.after(400, self._register_global_hotkeys)
        self._monitor_audio_devices()

    # ── Stealth ───────────────────────────────────────────────────────────────
    def _activate_stealth(self):
        ok = apply_stealth(get_hwnd(self.root))
        self._set_status("🟢 ON" if ok else "🔴 STEALTH FAILED",
                         C["green"] if ok else C["accent"])

    # ── Hotkeys ───────────────────────────────────────────────────────────────
    def _register_global_hotkeys(self):
        try:
            import keyboard as kb
            kb.add_hotkey("ctrl+shift+h", self._toggle_visibility)
            kb.add_hotkey("ctrl+shift+s", self._start_snip)
            kb.add_hotkey("ctrl+shift+f", self._start_fullscreen)
            kb.add_hotkey("ctrl+shift+m", self._toggle_meeting)
            kb.add_hotkey("ctrl+shift+v", self._toggle_voice)
            self._log("system",
                "⌨️  Hotkeys: H=hide  S=snip  F=full  M=meeting  V=voice\n\n")
        except Exception as e:
            self._log("error", f"⚠️  Hotkeys failed: {e}\n\n")

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
            "StealthAI v11 — Deepgram Live STT\n"
            "🎙️  Toggle STT engine in toolbar: Deepgram (live) ↔ Whisper (offline)\n"
            "🔑  Add Deepgram key in API Key panel\n"
            "🎤  Voice: your mic   👂  Meeting: speaker/BT\n"
            "Both send after 3 s silence. Words appear live with Deepgram.\n\n"
        )

    # ── Header ────────────────────────────────────────────────────────────────
    def _build_header(self):
        hdr = tk.Frame(self.root, bg=C["panel"], pady=6)
        hdr.pack(fill=tk.X)
        left = tk.Frame(hdr, bg=C["panel"])
        left.pack(side=tk.LEFT, padx=6)
        self.menu_btn = tk.Button(left, text="☰", command=self._toggle_toolbar,
            bg=C["panel"], fg=C["yellow"], bd=0, font=("Segoe UI", 14),
            cursor="hand2", activebackground=C["panel"], activeforeground=C["accent"])
        self.menu_btn.pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(left, text="🕵️  Vimlesh AI", font=("Segoe UI", 12, "bold"),
            bg=C["panel"], fg=C["accent"]).pack(side=tk.LEFT)
        right = tk.Frame(hdr, bg=C["panel"])
        right.pack(side=tk.RIGHT, padx=6)
        tk.Button(right, text="🌓", command=self._toggle_theme,
            bg=C["panel"], fg=C["grey"], bd=0, font=("Segoe UI", 11),
            cursor="hand2", activebackground=C["panel"],
            activeforeground=C["accent"]).pack(side=tk.RIGHT, padx=2)
        for sym, cmd, col in [("✕", self.root.destroy, C["accent"]),
                               ("—", self.root.iconify,  C["grey"])]:
            tk.Button(right, text=sym, command=cmd, bg=C["panel"], fg=col,
                bd=0, font=("Segoe UI", 11), cursor="hand2",
                activebackground=C["panel"],
                activeforeground=C["accent"]).pack(side=tk.RIGHT, padx=2)
        self.status_dot = tk.Label(right, text="⏳", font=("Segoe UI", 9),
            bg=C["panel"], fg=C["grey"])
        self.status_dot.pack(side=tk.RIGHT, padx=6)

    def _set_status(self, msg, color=None):
        color = color or C["grey"]
        dot = "🟢" if "ON" in msg else ("🔴" if "FAIL" in msg else "⏳")
        self.status_dot.config(text=dot, fg=color)
        self.root.title(f"Winconfg  ·  {msg}")

    # ── Icon Toolbar ──────────────────────────────────────────────────────────
    def _build_icon_toolbar(self):
        self.toolbar = tk.Frame(self.root, bg=C["icon_bg"], pady=4)
        icons = [
            ("🔑", "API Keys",         self._panel_api_key),
            ("🤖", "Provider",         self._panel_provider),
            ("🔆", "Opacity",          self._panel_opacity),
            ("✂️", "Snip",             self._start_snip),
            ("📷", "Full Screen",      self._start_fullscreen),
            ("👂", "Meeting ON/OFF",   self._toggle_meeting),
            ("🎤", "Voice ON/OFF",     self._toggle_voice),
            ("🎙️", "STT Engine",       self._toggle_stt_engine),   # NEW
            ("🙈", "Hide",             self._toggle_visibility),
            ("🗑", "Clear",            self._clear_chat),
            ("⌨️", "Shortcuts",        self._panel_shortcuts),
            ("🪶", "Compact",          self._toggle_compact_mode),
            ("🔊", "Audio Device",     self._panel_audio_device),
            ("🌓", "Theme",            self._toggle_theme),
        ]
        self._toolbar_btns = {}
        for emoji, tip, cmd in icons:
            btn = tk.Button(self.toolbar, text=emoji, command=cmd,
                bg=C["icon_bg"], fg=C["white"], bd=0, font=("Segoe UI", 15),
                cursor="hand2", padx=5, pady=2,
                activebackground=C["panel"], activeforeground=C["yellow"],
                relief=tk.FLAT)
            btn.pack(side=tk.LEFT, expand=True)
            self._add_tooltip(btn, tip)
            self._toolbar_btns[emoji] = btn
        # Highlight STT button to show current engine
        self._update_stt_btn_label()
        self.toolbar_sep = tk.Frame(self.root, bg=C["dim"], height=1)

    def _toggle_stt_engine(self):
        if self.stt_engine.get() == "deepgram":
            self.stt_engine.set("whisper")
        else:
            self.stt_engine.set("deepgram")
        self._update_stt_btn_label()   # button color + tooltip only — no chat message

    def _update_stt_btn_label(self):
        if "🎙️" in self._toolbar_btns:
            if self.stt_engine.get() == "deepgram":
                self._toolbar_btns["🎙️"].config(fg=C["green"])
            else:
                self._toolbar_btns["🎙️"].config(fg=C["yellow"])

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

    # ── Input area ────────────────────────────────────────────────────────────
    def _build_input_area(self):
        lbl_row = tk.Frame(self.root, bg=C["panel"], pady=3)
        lbl_row.pack(fill=tk.X, padx=8, pady=(6, 0))
        tk.Label(lbl_row, text="✍️  Your question:", font=("Segoe UI", 9, "bold"),
            bg=C["panel"], fg=C["white"]).pack(side=tk.LEFT, padx=6)
        tk.Label(lbl_row, text="Ctrl+Enter = send",
            font=("Segoe UI", 7), bg=C["panel"], fg=C["grey"]
            ).pack(side=tk.RIGHT, padx=6)
        inp = tk.Frame(self.root, bg=C["panel"])
        inp.pack(fill=tk.X, padx=8)
        self.input_box = tk.Text(inp, height=3, font=("Segoe UI", 11),
            bg=C["input"], fg=C["white"], insertbackground=C["white"],
            relief=tk.FLAT, bd=6, wrap=tk.WORD)
        self.input_box.pack(fill=tk.X)
        self.input_box.bind("<Escape>", lambda e: self.root.iconify())
        self.send_btn = tk.Button(inp, text="  ➤  Send Message  [Ctrl+Enter]",
            command=self._send, bg=C["accent"], fg="white", bd=0,
            font=("Segoe UI", 10, "bold"), pady=6, cursor="hand2",
            activebackground="#c73652")
        self.send_btn.pack(fill=tk.X, pady=(4, 6))
        tk.Frame(self.root, bg=C["dim"], height=1).pack(fill=tk.X, padx=8)

    # ── Response area ─────────────────────────────────────────────────────────
    def _build_response_area(self):
        top = tk.Frame(self.root, bg=C["bg"])
        top.pack(fill=tk.X, padx=8, pady=(3, 0))
        tk.Label(top, text="🤖  AI Response", font=("Segoe UI", 8, "bold"),
            bg=C["bg"], fg=C["dim"]).pack(side=tk.LEFT)

        # A- A+ font size buttons
        tk.Button(top, text="A+", command=lambda: self._resize_chat_font(1),
            bg=C["bg"], fg=C["grey"], bd=0, font=("Segoe UI", 8, "bold"),
            cursor="hand2", activebackground=C["bg"],
            activeforeground=C["white"]).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(top, text="A-", command=lambda: self._resize_chat_font(-1),
            bg=C["bg"], fg=C["grey"], bd=0, font=("Segoe UI", 8, "bold"),
            cursor="hand2", activebackground=C["bg"],
            activeforeground=C["white"]).pack(side=tk.LEFT, padx=(2, 0))
        self._font_size_lbl = tk.Label(top, text=f"{self._chat_font_size}pt",
            font=("Segoe UI", 7), bg=C["bg"], fg=C["dim"])
        self._font_size_lbl.pack(side=tk.LEFT, padx=(4, 0))

        self.mtg_indicator = tk.Label(top, text="", font=("Segoe UI", 7),
            bg=C["bg"], fg=C["green"])
        self.mtg_indicator.pack(side=tk.RIGHT, padx=(4, 0))
        self.voice_indicator = tk.Label(top, text="", font=("Segoe UI", 7),
            bg=C["bg"], fg=C["blue"])
        self.voice_indicator.pack(side=tk.RIGHT, padx=(4, 0))
        self.main_pane = tk.PanedWindow(self.root, orient=tk.VERTICAL,
            bg=C["bg"], sashrelief=tk.RAISED, sashwidth=4)
        self.main_pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2, 8))
        self.chat_frame = tk.Frame(self.main_pane, bg=C["bg"])
        self.main_pane.add(self.chat_frame, stretch="always")
        self.chat = scrolledtext.ScrolledText(self.chat_frame, wrap=tk.WORD,
            state=tk.DISABLED, font=("Segoe UI", self._chat_font_size),
            bg=C["bg"], fg=C["white"],
            relief=tk.FLAT, padx=10, pady=6, selectbackground=C["input"])
        self.chat.pack(fill=tk.BOTH, expand=True)
        self.chat.tag_config("user",   foreground=C["blue"],
            font=("Segoe UI", self._chat_font_size, "bold"))
        self.chat.tag_config("ai",     foreground=C["green"],
            font=("Segoe UI", self._chat_font_size))
        self.chat.tag_config("label",  foreground=C["accent"],
            font=("Segoe UI", max(self._chat_font_size - 1, 7), "bold"))
        self.chat.tag_config("system", foreground=C["dim"],
            font=("Segoe UI", max(self._chat_font_size - 2, 7), "italic"))
        self.chat.tag_config("error",  foreground=C["red"],
            font=("Segoe UI", max(self._chat_font_size - 1, 7)))
        self.chat.tag_config("heard",  foreground=C["yellow"],
            font=("Segoe UI", max(self._chat_font_size - 1, 7), "italic"))

    def _resize_chat_font(self, delta):
        """Increase or decrease chat font size. Range 7–16."""
        new_size = max(7, min(16, self._chat_font_size + delta))
        if new_size == self._chat_font_size:
            return
        self._chat_font_size = new_size
        # Update widget font and all tags live — no rebuild needed
        self.chat.config(font=("Segoe UI", new_size))
        self.chat.tag_config("user",   font=("Segoe UI", new_size, "bold"))
        self.chat.tag_config("ai",     font=("Segoe UI", new_size))
        self.chat.tag_config("label",  font=("Segoe UI", max(new_size-1, 7), "bold"))
        self.chat.tag_config("system", font=("Segoe UI", max(new_size-2, 7), "italic"))
        self.chat.tag_config("error",  font=("Segoe UI", max(new_size-1, 7)))
        self.chat.tag_config("heard",  font=("Segoe UI", max(new_size-1, 7), "italic"))
        self._font_size_lbl.config(text=f"{new_size}pt")

    # ── Transcript panel ──────────────────────────────────────────────────────
    def _build_transcript_panel(self):
        self.transcript_frame = tk.Frame(self.main_pane, bg=C["panel"])
        self.main_pane.add(self.transcript_frame, stretch="never", height=140)
        header = tk.Frame(self.transcript_frame, bg=C["panel"])
        header.pack(fill=tk.X, pady=(2, 0))
        tk.Label(header, text="📝 Live Transcript", font=("Segoe UI", 8, "bold"),
            bg=C["panel"], fg=C["grey"]).pack(side=tk.LEFT, padx=2)
        # STT engine label shown in transcript header
        self.stt_label = tk.Label(header, text="", font=("Segoe UI", 7),
            bg=C["panel"], fg=C["green"])
        self.stt_label.pack(side=tk.RIGHT, padx=4)
        self.transcript = scrolledtext.ScrolledText(self.transcript_frame,
            wrap=tk.WORD, state=tk.DISABLED, height=6, font=("Consolas", 9),
            bg=C["input"], fg="#aaccff", relief=tk.FLAT, padx=6, pady=4,
            selectbackground=C["accent"])
        self.transcript.pack(fill=tk.BOTH, expand=True)
        self.transcript.tag_config("transcript", foreground="#aaccff")
        self.transcript.tag_config("live",       foreground="#ffffff")   # live partial words
        self.transcript.tag_config("final",      foreground="#aaccff")  # finalized line

    def _build_progress_bar(self):
        self.progress = ttk.Progressbar(self.root, mode="indeterminate",
            style="red.Horizontal.TProgressbar")
        ttk.Style().configure("red.Horizontal.TProgressbar",
                              background=C["progress"])

    # ── Theme ─────────────────────────────────────────────────────────────────
    def _toggle_theme(self):
        global C
        C.update(LIGHT if C == DARK else DARK)
        for w in self.root.winfo_children():
            w.destroy()
        self._build_ui()
        self.root.after(100, self._activate_stealth)

    # ── Compact mode ──────────────────────────────────────────────────────────
    def _toggle_compact_mode(self):
        self.compact_mode = not self.compact_mode
        if self.compact_mode:
            self.root.geometry("300x200")
            self.input_box.config(height=1)
            self.main_pane.pack_forget()
        else:
            self.root.geometry("540x700")
            self.input_box.config(height=3)
            self.main_pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2, 8))

    # ══════════════════════════════════════════════════════════════════════════
    # PANELS
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
        # Expanded panel: OpenAI/Claude key + Deepgram key
        p = self._make_panel("🔑 API Keys", 400, 200)

        def _key_row(parent, label, var, hint):
            tk.Label(parent, text=label, font=("Segoe UI", 9),
                bg=C["panel"], fg=C["grey"]).pack(anchor="w", padx=10, pady=(10, 2))
            row = tk.Frame(parent, bg=C["panel"])
            row.pack(fill=tk.X, padx=10)
            e = tk.Entry(row, textvariable=var, show="●",
                font=("Consolas", 9), bg=C["input"], fg=C["white"],
                insertbackground=C["white"], relief=tk.FLAT, bd=5)
            e.pack(side=tk.LEFT, fill=tk.X, expand=True)
            sv = tk.BooleanVar()
            tk.Checkbutton(row, text="👁", variable=sv,
                command=lambda: e.config(show="" if sv.get() else "●"),
                bg=C["panel"], fg=C["grey"], selectcolor=C["panel"],
                activebackground=C["panel"], bd=0).pack(side=tk.RIGHT, padx=4)
            tk.Label(parent, text=hint, font=("Segoe UI", 7),
                bg=C["panel"], fg=C["dim"]).pack(anchor="w", padx=10, pady=(0, 2))

        _key_row(p, "OpenAI / Claude Key:", self.api_key,
                 "👉  platform.openai.com/api-keys  or  console.anthropic.com")
        _key_row(p, "Deepgram Key (for live STT):", self.deepgram_key,
                 "👉  deepgram.com  →  Free 200 hrs/month")

    def _panel_provider(self):
        p = self._make_panel("🤖 Provider", 300, 110)
        tk.Label(p, text="Select AI Provider:", font=("Segoe UI", 9),
            bg=C["panel"], fg=C["grey"]).pack(anchor="w", padx=10, pady=(10, 4))
        tk.Radiobutton(p, text="ChatGPT  (gpt-4o-mini / gpt-4o)",
            variable=self.provider, value="openai",
            bg=C["panel"], fg=C["blue"], selectcolor=C["panel"],
            activebackground=C["panel"], font=("Segoe UI", 9, "bold"),
            cursor="hand2").pack(anchor="w", padx=20)
        tk.Radiobutton(p, text="Claude  (needs Anthropic key)",
            variable=self.provider, value="claude",
            bg=C["panel"], fg=C["dim"], selectcolor=C["panel"],
            activebackground=C["panel"], font=("Segoe UI", 9),
            cursor="hand2").pack(anchor="w", padx=20)

    def _panel_opacity(self):
        p = self._make_panel("🔆 Opacity", 300, 90)
        tk.Label(p, text="Drag to adjust:", font=("Segoe UI", 8),
            bg=C["panel"], fg=C["grey"]).pack(anchor="w", padx=10, pady=(8, 2))
        row = tk.Frame(p, bg=C["panel"])
        row.pack(fill=tk.X, padx=10)
        lbl = tk.Label(row, text=f"{int(self._alpha_val*100)}%",
            font=("Consolas", 9), bg=C["panel"], fg=C["blue"], width=4)
        lbl.pack(side=tk.RIGHT)
        def on_change(val):
            self._alpha_val = int(val) / 100.0
            self.root.attributes("-alpha", self._alpha_val)
            lbl.config(text=f"{int(val)}%")
        tk.Scale(row, from_=20, to=100, orient=tk.HORIZONTAL, command=on_change,
            bg=C["panel"], fg=C["grey"], troughcolor=C["input"],
            highlightthickness=0, bd=0, showvalue=False,
            sliderrelief=tk.FLAT, sliderlength=14
            ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

    def _panel_shortcuts(self):
        p = self._make_panel("⌨️ Shortcuts", 340, 240)
        shortcuts = [
            ("Ctrl+Enter",   "Send message"),
            ("Escape",       "Minimise"),
            ("Ctrl+Shift+H", "Hide/Show"),
            ("Ctrl+Shift+S", "Snip region"),
            ("Ctrl+Shift+F", "Full screen"),
            ("Ctrl+Shift+M", "Meeting ON/OFF"),
            ("Ctrl+Shift+V", "Voice ON/OFF"),
        ]
        tk.Label(p, text="⌨️  Shortcuts", font=("Segoe UI", 8, "bold"),
            bg=C["panel"], fg=C["grey"]).pack(anchor="w", padx=10, pady=(8, 4))
        grid = tk.Frame(p, bg=C["panel"])
        grid.pack(fill=tk.X, padx=10)
        for i, (key, desc) in enumerate(shortcuts):
            tk.Label(grid, text=key, font=("Consolas", 8, "bold"),
                bg=C["panel"], fg=C["blue"], width=16, anchor="w"
                ).grid(row=i, column=0, sticky="w", pady=1)
            tk.Label(grid, text=desc, font=("Segoe UI", 8),
                bg=C["panel"], fg=C["white"], anchor="w"
                ).grid(row=i, column=1, sticky="w", padx=6)

    def _panel_audio_device(self):
        p = self._make_panel("🔊 Audio Source", 460, 280)
        tk.Label(p, text="Select meeting audio source:",
            font=("Segoe UI", 9, "bold"), bg=C["panel"], fg=C["grey"]
            ).pack(anchor="w", padx=10, pady=(10, 4))
        tk.Label(p,
            text="💡 Bluetooth: set BT device as Default Playback Device first",
            font=("Segoe UI", 7), bg=C["panel"], fg=C["yellow"], wraplength=420
            ).pack(anchor="w", padx=10, pady=(0, 6))
        frame = tk.Frame(p, bg=C["panel"])
        frame.pack(fill=tk.BOTH, expand=True, padx=10)
        sb = tk.Scrollbar(frame)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        listbox = tk.Listbox(frame, font=("Consolas", 8), bg=C["input"],
            fg=C["white"], selectbackground=C["accent"], relief=tk.FLAT,
            yscrollcommand=sb.set, height=8)
        listbox.pack(fill=tk.BOTH, expand=True)
        sb.config(command=listbox.yview)

        def populate():
            com_init()
            try:
                import soundcard as sc
                devices = [("AUTO", "🔄  Auto-detect (recommended)")]
                for spk in sc.all_speakers():
                    lbl = (f"🔵  BT: {spk.name}"
                           if any(k in spk.name.lower()
                                  for k in ["bluetooth","bt ","wireless","headset","airpod"])
                           else f"🔊  {spk.name}")
                    devices.append((spk.name, lbl))
                for mic in sc.all_microphones(include_loopback=False):
                    devices.append((f"MIC:{mic.name}", f"🎤  {mic.name}"))
                def fill():
                    listbox.delete(0, tk.END)
                    for did, lbl in devices:
                        listbox.insert(tk.END, lbl)
                    for i, (did, _) in enumerate(devices):
                        if did == (self._selected_device or "AUTO"):
                            listbox.selection_set(i); listbox.see(i); break
                    listbox._devices = devices
                p.after(0, fill)
            except Exception as e:
                p.after(0, lambda: self._log("error", f"⚠️  Device list: {e}\n"))
            finally:
                com_uninit()
        threading.Thread(target=populate, daemon=True).start()

        btn_row = tk.Frame(p, bg=C["panel"])
        btn_row.pack(fill=tk.X, padx=10, pady=6)
        def apply_sel():
            sel = listbox.curselection()
            if sel and hasattr(listbox, "_devices"):
                did, lbl = listbox._devices[sel[0]]
                self._selected_device = None if did == "AUTO" else did
                self._log("system", f"🔊  Source: {lbl}\n")
                p.destroy()
        tk.Button(btn_row, text="✅  Use Selected", command=apply_sel,
            bg=C["accent"], fg="white", bd=0, font=("Segoe UI", 9, "bold"),
            pady=5, cursor="hand2").pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(btn_row, text="🔄 Refresh",
            command=lambda: threading.Thread(target=populate, daemon=True).start(),
            bg=C["panel"], fg=C["grey"], bd=1, font=("Segoe UI", 9),
            pady=5, cursor="hand2").pack(side=tk.RIGHT, padx=(6, 0))

    # ── Tooltip ───────────────────────────────────────────────────────────────
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
            if tip:
                tip.destroy(); tip = None
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
        self._live_line_start = None
        self._log("system", "Chat cleared.\n\n")

    def _lock_ui(self, locked):
        state = tk.DISABLED if locked else tk.NORMAL
        for w in (self.send_btn, self.input_box):
            w.config(state=state)
        if locked:
            self.progress.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=2)
            self.progress.start(10)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def _check_key(self):
        key = self.api_key.get().strip()
        if not key or key == "sk-...":
            self._log("error", "⚠️  Enter API key — click 🔑 in toolbar.\n\n")
            return False
        return True

    def _toggle_visibility(self):
        if self.visible:
            self.root.withdraw(); self.visible = False
        else:
            self.root.deiconify(); self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.attributes("-alpha", self._alpha_val)
            self.visible = True

    def _get_question(self):
        text = self.input_box.get("1.0", tk.END).strip()
        if text: self.input_box.delete("1.0", tk.END)
        return text or "Analyse this screenshot and describe what you see."

    # ── Send typed text ───────────────────────────────────────────────────────
    def _send(self):
        text = self.input_box.get("1.0", tk.END).strip()
        if not text or not self._check_key(): return
        self.input_box.delete("1.0", tk.END)
        self._log("user", f"\nYou:  {text}\n")
        self.history.append({"role": "user", "content": text})
        self._lock_ui(True)
        threading.Thread(target=self._call_text, daemon=True).start()

    def _call_text(self):
        com_init()
        try:
            if self.provider.get() == "openai": self._openai_text()
            else: self._claude_text()
        except Exception as e:
            self.root.after(0, self._log, "error", f"⚠️  {e}\n\n")
        finally:
            com_uninit()
            self.root.after(0, self._lock_ui, False)

    # ── Screenshots ───────────────────────────────────────────────────────────
    def _start_fullscreen(self):
        if not self._check_key(): return
        question = self._get_question()
        self._log("system", "📷  Capturing in 0.5 s…\n")
        self.root.withdraw()
        self.root.after(500, lambda: self._do_fullscreen(question))

    def _do_fullscreen(self, question):
        try:
            import mss
        except ImportError:
            self.root.deiconify()
            self._log("error", "⚠️  pip install mss\n\n"); return
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        self.root.deiconify(); self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self._alpha_val)
        self.visible = True
        ocr = self._extract_ocr(img)
        if ocr: question = f"OCR:\n{ocr}\n\nQuestion: {question}"
        w, h = img.size
        if w > 1280: img = img.resize((1280, int(h*1280/w)))
        buf = io.BytesIO(); img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        self._log("user", f"📷  [Full Screen]\n")
        self._lock_ui(True)
        threading.Thread(target=self._run_vision,
            args=(self.provider.get(), b64, question), daemon=True).start()

    def _start_snip(self):
        if not self._check_key(): return
        self._pending_q = self._get_question()
        self._log("system", "✂️  Draw selection…\n")
        self.root.withdraw()
        self.root.after(300, lambda: SnipOverlay(self.root, self._on_snip_done))

    def _on_snip_done(self, x, y, w, h):
        self.root.deiconify(); self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self._alpha_val)
        self.visible = True
        if x is None: self._log("system", "✂️  Cancelled.\n\n"); return
        try:
            import mss
        except ImportError:
            self._log("error", "⚠️  pip install mss\n\n"); return
        with mss.mss() as sct:
            raw = sct.grab({"left": x, "top": y, "width": w, "height": h})
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        ocr = self._extract_ocr(img)
        question = self._pending_q
        if ocr: question = f"OCR:\n{ocr}\n\nQuestion: {question}"
        buf = io.BytesIO(); img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        self._log("user", f"✂️  [Snip {w}×{h}]\n")
        self._lock_ui(True)
        threading.Thread(target=self._run_vision,
            args=(self.provider.get(), b64, question), daemon=True).start()

    def _extract_ocr(self, img):
        try:
            import pytesseract
            return pytesseract.image_to_string(img).strip()
        except Exception:
            return ""

    def _run_vision(self, provider, b64, question):
        com_init()
        try:
            if provider == "openai": self._openai_vision(b64, question)
            else: self._claude_vision(b64, question)
        except Exception as e:
            self.root.after(0, self._log, "error", f"⚠️  {e}\n\n")
        finally:
            com_uninit()
            self.root.after(0, self._lock_ui, False)

    # ── OpenAI ────────────────────────────────────────────────────────────────
    def _openai_text(self, max_tokens=1024, system_override=None):
        import openai
        client = openai.OpenAI(api_key=self.api_key.get().strip())
        system = system_override or "You are a concise AI assistant."
        msgs   = [{"role": "system", "content": system}] + self.history
        self.root.after(0, self._log, "label", "\nChatGPT:  ")
        full = ""
        for chunk in client.chat.completions.create(
            model=OPENAI_CHAT_MODEL, messages=msgs, stream=True, max_tokens=max_tokens
        ):
            ch = chunk.choices[0].delta.content or ""
            if ch:
                full += ch
                self.root.after(0, self._stream_char, ch)
        self.root.after(0, self._log, "ai", "\n\n")
        self.history.append({"role": "assistant", "content": full})

    def _openai_vision(self, b64, question):
        import openai
        client = openai.OpenAI(api_key=self.api_key.get().strip())
        self.root.after(0, self._log, "label", "\nChatGPT (Vision):  ")
        resp = client.chat.completions.create(
            model=OPENAI_VISION_MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
                {"type": "text", "text": question}
            ]}])
        full = resp.choices[0].message.content or ""
        for ch in full: self.root.after(0, self._stream_char, ch)
        self.root.after(0, self._log, "ai", "\n\n")
        self.history.append({"role": "user",      "content": f"[Screenshot] {question}"})
        self.history.append({"role": "assistant", "content": full})

    # ── Claude ────────────────────────────────────────────────────────────────
    def _claude_text(self, max_tokens=1024, system_override=None):
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key.get().strip())
        system = system_override or "You are a concise AI assistant."
        self.root.after(0, self._log, "label", "\nClaude:  ")
        full = ""
        with client.messages.stream(
            model=CLAUDE_CHAT_MODEL, max_tokens=max_tokens,
            system=system, messages=self.history
        ) as stream:
            for ch in stream.text_stream:
                full += ch; self.root.after(0, self._stream_char, ch)
        self.root.after(0, self._log, "ai", "\n\n")
        self.history.append({"role": "assistant", "content": full})

    def _claude_vision(self, b64, question):
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key.get().strip())
        self.root.after(0, self._log, "label", "\nClaude (Vision):  ")
        full = ""
        with client.messages.stream(
            model=CLAUDE_CHAT_MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": question}
            ]}]
        ) as stream:
            for ch in stream.text_stream:
                full += ch; self.root.after(0, self._stream_char, ch)
        self.root.after(0, self._log, "ai", "\n\n")
        self.history.append({"role": "user",      "content": f"[Screenshot] {question}"})
        self.history.append({"role": "assistant", "content": full})

    # ══════════════════════════════════════════════════════════════════════════
    # 🎙️  LIVE TRANSCRIPT HELPERS
    # ══════════════════════════════════════════════════════════════════════════
    def _transcript_live_update(self, partial_text):
        """Update the current live line with partial words from Deepgram."""
        self.transcript.config(state=tk.NORMAL)
        if self._live_line_start is None:
            # Start a new live line
            ts = datetime.now().strftime("%H:%M:%S")
            self.transcript.insert(tk.END, f"[{ts}] ", "final")
            self._live_line_start = self.transcript.index(tk.END)
        else:
            # Delete previous partial text on this line
            self.transcript.delete(self._live_line_start, tk.END)
        self.transcript.insert(tk.END, partial_text + "…", "live")
        self.transcript.see(tk.END)
        self.transcript.config(state=tk.DISABLED)

    def _transcript_finalize_line(self, final_text):
        """Replace live partial line with finalized text."""
        self.transcript.config(state=tk.NORMAL)
        if self._live_line_start is not None:
            self.transcript.delete(self._live_line_start, tk.END)
            self.transcript.insert(tk.END, final_text + "\n", "final")
        else:
            ts = datetime.now().strftime("%H:%M:%S")
            self.transcript.insert(tk.END, f"[{ts}] {final_text}\n", "final")
        self._live_line_start = None
        self.transcript.see(tk.END)
        self.transcript.config(state=tk.DISABLED)

    # ══════════════════════════════════════════════════════════════════════════
    # 🎙️  DEEPGRAM LIVE STT
    # ══════════════════════════════════════════════════════════════════════════
    def _run_deepgram_loop(self, mic_obj, source_label,
                           is_running_fn, indicator_label, mode_tag):
        """
        Streams audio to Deepgram WebSocket.
        Words appear live on transcript panel.
        Accumulates final transcripts; sends to AI after 3 s silence.
        Falls back to Whisper on any error.
        """
        dg_key = self.deepgram_key.get().strip()
        if not dg_key:
            self.root.after(0, self._log, "error",
                "⚠️  No Deepgram key — add it in 🔑 panel. Using Whisper.\n\n")
            self._run_whisper_loop(mic_obj, source_label,
                                   is_running_fn, indicator_label, mode_tag)
            return

        try:
            import websocket as ws_lib
        except ImportError:
            self.root.after(0, self._log, "error",
                "⚠️  pip install websocket-client — falling back to Whisper.\n\n")
            self._run_whisper_loop(mic_obj, source_label,
                                   is_running_fn, indicator_label, mode_tag)
            return

        self.root.after(0, self._log, "system",
            f"{mode_tag}  Deepgram LIVE on: {source_label}\n"
            f"     Words appear as you speak. Sends after {int(SEND_SILENCE_SEC)} s silence.\n\n")
        self.root.after(0, self.stt_label.config, {"text": "🟢 Deepgram LIVE"})

        # Accumulated final text + silence tracking
        accumulated_text   = []
        silence_timer      = [0.0]   # mutable for closure
        speech_active      = [False]
        ws_conn            = [None]
        audio_q            = queue.Queue()
        fallback_triggered = [False]

        # ── WebSocket callbacks ────────────────────────────────────────────────
        def on_message(ws, message):
            try:
                data = json.loads(message)
                alt  = data.get("channel", {}).get("alternatives", [{}])[0]
                transcript = alt.get("transcript", "").strip()
                is_final   = data.get("is_final", False)
                if not transcript:
                    return
                if is_final:
                    accumulated_text.append(transcript)
                    silence_timer[0]  = 0.0
                    speech_active[0]  = True
                    full_so_far = " ".join(accumulated_text)
                    self.root.after(0, self._transcript_finalize_line, full_so_far)
                    self.root.after(0, indicator_label.config,
                                    {"text": f"{mode_tag} SPEAKING"})
                else:
                    # Partial — show live
                    preview = " ".join(accumulated_text + [transcript])
                    self.root.after(0, self._transcript_live_update, preview)
            except Exception:
                pass

        def on_error(ws, error):
            if not fallback_triggered[0]:
                fallback_triggered[0] = True
                self.root.after(0, self._log, "system",
                    "⚠️ Deepgram unavailable — switched to Whisper\n\n")
                self.root.after(0, self.stt_label.config,
                                {"text": "🟡 Whisper fallback"})

        def on_close(ws, *args):
            pass

        def on_open(ws):
            ws_conn[0] = ws
            # Drain audio queue into websocket
            def sender():
                while is_running_fn() and not fallback_triggered[0]:
                    try:
                        pcm = audio_q.get(timeout=0.1)
                        ws.send(pcm, opcode=0x2)  # binary
                    except queue.Empty:
                        pass
                    except Exception:
                        break
            threading.Thread(target=sender, daemon=True).start()

        # ── Connect Deepgram WebSocket ─────────────────────────────────────────
        dg_url = (f"wss://api.deepgram.com/v1/listen"
                  f"?encoding=linear16&sample_rate={SAMPLERATE}"
                  f"&channels=1&model=nova-2&interim_results=true"
                  f"&endpointing=false")
        ws_app = ws_lib.WebSocketApp(
            dg_url,
            header={"Authorization": f"Token {dg_key}"},
            on_open=on_message and on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        ws_thread = threading.Thread(
            target=lambda: ws_app.run_forever(), daemon=True)
        ws_thread.start()
        time.sleep(0.5)  # let WS connect

        if fallback_triggered[0]:
            self._run_whisper_loop(mic_obj, source_label,
                                   is_running_fn, indicator_label, mode_tag)
            return

        # ── Audio capture loop + silence timer ────────────────────────────────
        SILENCE_CHECK_SEC = 0.1   # how often we check silence accumulation
        silence_accumulator = 0.0

        try:
            with mic_obj.recorder(samplerate=SAMPLERATE, channels=1) as rec:
                while is_running_fn() and not fallback_triggered[0]:
                    data = rec.record(numframes=CHUNK_FRAMES)
                    mono = data[:, 0] if data.ndim > 1 else data
                    if len(mono) != CHUNK_FRAMES:
                        mono = mono[:CHUNK_FRAMES] if len(mono) > CHUNK_FRAMES \
                               else np.pad(mono, (0, CHUNK_FRAMES - len(mono)))

                    # Send raw PCM to Deepgram
                    pcm_bytes = (np.clip(mono, -1, 1) * 32767
                                 ).astype(np.int16).tobytes()
                    audio_q.put(pcm_bytes)

                    # Track silence for send trigger
                    rms = float(np.sqrt(np.mean(mono**2)))
                    if rms > SILENCE_THRESH:
                        silence_accumulator = 0.0
                        speech_active[0]    = True
                    else:
                        if speech_active[0]:
                            silence_accumulator += CHUNK_SEC
                            if silence_accumulator >= SEND_SILENCE_SEC:
                                # 3 s silence hit — send accumulated text to AI
                                full_text = " ".join(accumulated_text).strip()
                                if full_text and len(full_text) > 1:
                                    self.root.after(0, indicator_label.config,
                                                    {"text": f"{mode_tag} SENDING…"})
                                    self._send_to_ai(full_text)
                                accumulated_text.clear()
                                silence_accumulator = 0.0
                                speech_active[0]    = False
                                self._live_line_start = None
                                self.root.after(0, indicator_label.config,
                                                {"text": f"{mode_tag} LIVE"})

        except Exception as e:
            self.root.after(0, self._log, "error", f"⚠️  Audio error: {e}\n")

        finally:
            try:
                ws_app.close()
            except Exception:
                pass

        # If we exited due to fallback, run Whisper loop
        if fallback_triggered[0] and is_running_fn():
            self._run_whisper_loop(mic_obj, source_label,
                                   is_running_fn, indicator_label, mode_tag)

    # ══════════════════════════════════════════════════════════════════════════
    # 🎙️  WHISPER ACCUMULATOR LOOP  (fallback / manual selection)
    # ══════════════════════════════════════════════════════════════════════════
    def _run_whisper_loop(self, mic_obj, source_label,
                          is_running_fn, indicator_label, mode_tag):
        """
        Original VAD-based accumulation loop using local Whisper.
        Used when Deepgram is off or unavailable.
        """
        self.root.after(0, self.stt_label.config, {"text": "🟡 Whisper"})
        try:
            from webrtcvad import Vad
            vad = Vad(2)
        except Exception as e:
            self.root.after(0, self._log, "error",
                f"⚠️  webrtcvad missing: pip install webrtcvad-wheels\n{e}\n")
            return

        self.root.after(0, self._log, "system",
            f"{mode_tag}  Whisper on: {source_label}\n"
            f"     Sends after {int(SEND_SILENCE_SEC)} s silence.\n\n")

        all_frames  = []
        silence_dur = 0.0
        speech_dur  = 0.0
        is_speaking = False
        pre_buf     = []

        while is_running_fn():
            try:
                with mic_obj.recorder(samplerate=SAMPLERATE, channels=1) as rec:
                    while is_running_fn():
                        try:
                            data = rec.record(numframes=CHUNK_FRAMES)
                        except Exception:
                            break
                        mono = data[:, 0] if data.ndim > 1 else data
                        if len(mono) != CHUNK_FRAMES:
                            mono = mono[:CHUNK_FRAMES] if len(mono) > CHUNK_FRAMES \
                                   else np.pad(mono, (0, CHUNK_FRAMES - len(mono)))

                        pcm = (np.clip(mono, -1, 1) * 32767).astype(np.int16)
                        try:
                            is_speech = vad.is_speech(pcm.tobytes(), SAMPLERATE)
                        except Exception:
                            is_speech = float(np.sqrt(np.mean(mono**2))) > SILENCE_THRESH

                        if is_speech:
                            if not is_speaking:
                                is_speaking = True
                                all_frames.extend(pre_buf[-int(0.3/CHUNK_SEC):])
                                pre_buf.clear()
                                self.root.after(0, indicator_label.config,
                                                {"text": f"{mode_tag} SPEAKING"})
                            all_frames.append(mono)
                            speech_dur  += CHUNK_SEC
                            silence_dur  = 0.0
                        else:
                            if is_speaking:
                                all_frames.append(mono)
                                silence_dur += CHUNK_SEC
                                if silence_dur >= SEND_SILENCE_SEC:
                                    if speech_dur >= MIN_SPEECH_SEC:
                                        self.root.after(0, indicator_label.config,
                                                        {"text": f"{mode_tag} SENDING…"})
                                        self._flush_whisper_and_send(list(all_frames))
                                    all_frames.clear()
                                    silence_dur = 0.0
                                    speech_dur  = 0.0
                                    is_speaking = False
                                    pre_buf.clear()
                                    self.root.after(0, indicator_label.config,
                                                    {"text": f"{mode_tag} LIVE"})
                            else:
                                pre_buf.append(mono)
                                if len(pre_buf) > int(0.3/CHUNK_SEC):
                                    pre_buf.pop(0)

                        if speech_dur >= MAX_SPEECH_SEC:
                            self.root.after(0, indicator_label.config,
                                            {"text": f"{mode_tag} SENDING…"})
                            self._flush_whisper_and_send(list(all_frames))
                            all_frames.clear()
                            silence_dur = 0.0; speech_dur = 0.0
                            is_speaking = False; pre_buf.clear()
                            self.root.after(0, indicator_label.config,
                                            {"text": f"{mode_tag} LIVE"})

            except Exception as e:
                if not is_running_fn(): break
                self.root.after(0, self._log, "error",
                    f"⚠️  Audio error: {e}\n🔄  Reconnecting in 2 s…\n")
                time.sleep(2)
                new_mic, new_label, _ = self._resolve_audio_source(
                    use_mic=(mode_tag == "🎤"))
                if new_mic:
                    mic_obj = new_mic
                    self.root.after(0, self._log, "system",
                        f"🔄  Reconnected: {new_label}\n")
                else:
                    self.root.after(0, self._log, "error",
                        "⚠️  Could not reconnect. Stopping.\n")
                    break

    def _flush_whisper_and_send(self, frames):
        """Transcribe with Whisper then send to AI."""
        if not frames: return
        audio = np.concatenate(frames)
        try:
            import noisereduce as nr
            audio = nr.reduce_noise(y=audio, sr=SAMPLERATE)
        except Exception:
            pass
        text = self._transcribe_whisper(audio)
        if not text:
            text = self._google_stt_fallback(audio)
        if not text: return
        try:
            text = get_punctuator().infer([text])[0]
        except Exception:
            pass
        self.root.after(0, self._transcript_finalize_line, text)
        self._send_to_ai(text)

    # ══════════════════════════════════════════════════════════════════════════
    # 🤖  SEND TO AI  (shared by both Deepgram and Whisper paths)
    # ══════════════════════════════════════════════════════════════════════════
    def _send_to_ai(self, text):
        """Send transcribed text to AI. Called from audio threads."""
        self.root.after(0, self._log, "heard", f'🎧  "{text}"\n')
        sys_msg = (
            "You are a smart, concise AI assistant helping during a meeting or interview. "
            "The user's speech was transcribed and sent to you. "
            "Answer the question or respond naturally and helpfully. "
            "Keep answers concise (2-4 sentences) unless detail is clearly needed. "
            "If the input is a greeting like 'Hello', respond politely and briefly."
        )
        self.history.append({"role": "user", "content": text})
        try:
            if self.provider.get() == "openai":
                self._openai_text(max_tokens=400, system_override=sys_msg)
            else:
                self._claude_text(max_tokens=400, system_override=sys_msg)
        except Exception as e:
            self.root.after(0, self._log, "error", f"⚠️  AI error: {e}\n\n")

    # ══════════════════════════════════════════════════════════════════════════
    # 🎤  VOICE INPUT  — mic source
    # ══════════════════════════════════════════════════════════════════════════
    def _toggle_voice(self):
        if not self.visible: self._toggle_visibility()
        if self.voice_on:
            self.voice_on = False
            self.voice_indicator.config(text="")
            if "🎤" in self._toolbar_btns:
                self._toolbar_btns["🎤"].config(fg=C["white"])
            self.root.after(0, self.stt_label.config, {"text": ""})
            self._log("system", "🎤  Voice input stopped.\n\n")
        else:
            if not self._check_key(): return
            self.voice_on = True
            self.voice_indicator.config(text="🎤 LIVE")
            if "🎤" in self._toolbar_btns:
                self._toolbar_btns["🎤"].config(fg=C["green"])
            self._log("system", "🎤  Voice input ON — microphone.\n")
            self.transcript.config(state=tk.NORMAL)
            self.transcript.delete("1.0", tk.END)
            self.transcript.config(state=tk.DISABLED)
            self._live_line_start = None
            threading.Thread(target=self._voice_thread, daemon=True).start()

    def _start_voice_input(self):
        self._toggle_voice()

    def _voice_thread(self):
        com_init()
        try:
            mic, label, _ = self._resolve_audio_source(use_mic=True)
            if mic is None:
                self.root.after(0, self._log, "error",
                    f"⚠️  Microphone error: {label}\n")
                self.root.after(0, self._stop_voice_safe)
                return
            if self.stt_engine.get() == "deepgram":
                self._run_deepgram_loop(mic, label,
                    lambda: self.voice_on, self.voice_indicator, "🎤")
            else:
                self._run_whisper_loop(mic, label,
                    lambda: self.voice_on, self.voice_indicator, "🎤")
        finally:
            com_uninit()
            self.root.after(0, self._stop_voice_safe)

    def _stop_voice_safe(self):
        self.voice_on = False
        self.voice_indicator.config(text="")
        self.stt_label.config(text="")
        if "🎤" in self._toolbar_btns:
            self._toolbar_btns["🎤"].config(fg=C["white"])

    # ══════════════════════════════════════════════════════════════════════════
    # 👂  MEETING LISTENER  — speaker loopback source
    # ══════════════════════════════════════════════════════════════════════════
    def _toggle_meeting(self):
        if not self.visible: self._toggle_visibility()
        if self.meeting_on:
            self.meeting_on = False
            self.mtg_indicator.config(text="")
            if "👂" in self._toolbar_btns:
                self._toolbar_btns["👂"].config(fg=C["white"])
            self.root.after(0, self.stt_label.config, {"text": ""})
            self._log("system", "👂  Meeting listener stopped.\n\n")
        else:
            if not self._check_key(): return
            self.meeting_on = True
            self.mtg_indicator.config(text="👂 LIVE")
            if "👂" in self._toolbar_btns:
                self._toolbar_btns["👂"].config(fg=C["green"])
            self._log("system", "👂  Meeting listener ON — speaker loopback.\n")
            self.transcript.config(state=tk.NORMAL)
            self.transcript.delete("1.0", tk.END)
            self.transcript.config(state=tk.DISABLED)
            self._live_line_start = None
            threading.Thread(target=self._meeting_thread, daemon=True).start()

    def _meeting_thread(self):
        com_init()
        try:
            mic, label, _ = self._resolve_audio_source(use_mic=False)
            if mic is None:
                self.root.after(0, self._log, "error",
                    f"⚠️  Audio source error: {label}\n"
                    "💡  Set BT as Default Playback Device\n"
                    "💡  Enable Stereo Mix in Sound → Recording\n"
                    "💡  Pick device via 🔊 in toolbar\n\n")
                self.root.after(0, self._stop_meeting_safe)
                return
            if self.stt_engine.get() == "deepgram":
                self._run_deepgram_loop(mic, label,
                    lambda: self.meeting_on, self.mtg_indicator, "👂")
            else:
                self._run_whisper_loop(mic, label,
                    lambda: self.meeting_on, self.mtg_indicator, "👂")
        finally:
            com_uninit()
            self.root.after(0, self._stop_meeting_safe)

    def _stop_meeting_safe(self):
        self.meeting_on = False
        self.mtg_indicator.config(text="")
        self.stt_label.config(text="")
        if "👂" in self._toolbar_btns:
            self._toolbar_btns["👂"].config(fg=C["white"])

    # ── Resolve audio source ──────────────────────────────────────────────────
    def _resolve_audio_source(self, use_mic=False):
        try:
            import soundcard as sc
            if self._selected_device and not use_mic:
                if self._selected_device.startswith("MIC:"):
                    name = self._selected_device[4:]
                    return sc.get_microphone(id=name, include_loopback=False), \
                           f"🎤 {name}", False
                else:
                    loop = sc.get_microphone(
                        id=self._selected_device, include_loopback=True)
                    return loop, f"🔊 {self._selected_device}", True
            if use_mic:
                mic = sc.default_microphone()
                return mic, f"🎤 {mic.name}", False
            else:
                return get_best_audio_source()
        except Exception as e:
            return None, str(e), False

    # ── Audio device monitor ──────────────────────────────────────────────────
    def _monitor_audio_devices(self):
        def check():
            self._check_audio_device_change()
            self.root.after(3000, check)
        self.root.after(3000, check)

    def _check_audio_device_change(self):
        def worker():
            com_init()
            try:
                import soundcard as sc
                state = "|".join(sorted(s.name for s in sc.all_speakers()))
            except Exception:
                state = ""
            finally:
                com_uninit()
            if state and state != self._last_audio_state:
                if self._last_audio_state:
                    self.root.after(0, self._on_audio_device_changed)
                self._last_audio_state = state
        threading.Thread(target=worker, daemon=True).start()

    def _on_audio_device_changed(self):
        self._log("system", "🔄  Audio devices changed (Bluetooth?)\n")
        if self.meeting_on:
            self._log("system", "🔄  Restarting meeting listener…\n")
            self.meeting_on = False
            self.root.after(1500, self._toggle_meeting)
        if self.voice_on:
            self._log("system", "🔄  Restarting voice input…\n")
            self.voice_on = False
            self.root.after(1500, self._toggle_voice)

    # ── Transcription helpers ─────────────────────────────────────────────────
    def _transcribe_whisper(self, audio):
        try:
            model = get_whisper_model()
            segs, _ = model.transcribe(audio.astype(np.float32),
                                       language="en", beam_size=5)
            return " ".join(s.text for s in segs).strip()
        except Exception as e:
            print(f"Whisper: {e}")
            return ""

    def _google_stt_fallback(self, audio):
        try:
            import speech_recognition as sr
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2)
                wf.setframerate(SAMPLERATE); wf.writeframes(pcm.tobytes())
            buf.seek(0)
            r = sr.Recognizer()
            return r.recognize_google(sr.AudioData(buf.read(), SAMPLERATE, 2))
        except Exception:
            return ""

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if sys.platform != "win32":
        print("⚠️  Stealth mode is Windows-only.")
    StealthAI().run()

