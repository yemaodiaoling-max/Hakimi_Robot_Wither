"""Hakimi Q-Version Round-Faced Desktop Pet (Windows Local UI).

Features:
- Frameless, transparent, always-on-top draggable Q-version round-faced Hakimi widget
- Multi-state animations (idle, peek, thinking, happy) with floating speech bubbles
- Screen vision (on-demand + attach-on-chat + optional periodic screen peek)
- Connected to remote VPS /opt/hakimi/app/desktop_pet_agent.py (gemini-4-hydrogen + Antigravity Waiter)
- Shared long-term MemoryStore + quantitative context clearing with memory consolidation
- Anti-prompt-injection local PC action approval cards (Human-in-the-loop gate)
- One-click Windows Startup toggle for effortless daily boot
"""
from __future__ import annotations

import ctypes
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
import base64
import io
import uuid
from PIL import Image, ImageGrab, ImageTk

# Ensure parent repo is importable
PET_DIR = Path(__file__).resolve().parent
ROOT_DIR = PET_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import random
from desktop_pet.client_bridge import ServerBridge, VoiceRecorder, capture_screen_b64, get_active_window_title
from desktop_pet.livehime_overlay import LivehimeServer
from desktop_pet.local_executor import execute_approved_action

ASSETS_DIR = PET_DIR / "assets"
UI_STATE_PATH = ROOT_DIR / "state" / "desktop_pet_ui.json"
STARTUP_LNK_NAME = "HakimiDesktopPet.lnk"
TRANSPARENT_KEY = "#010203"
OUTLINE_RGB = (48, 36, 74)

HEADPAT_BUBBLES = [
    "哼，看在你诚心顺毛的份上，本喵勉强呼噜两声~",
    "别把本喵的呆毛揉乱了喵！",
    "舒服是舒服…但小鱼干和罐头还是不能少！",
    "唔…下巴再顺两下…不对，本喵才没觉得舒服呢！",
    "呼噜噜…摸头可以，等下记得给本喵倒温水喝~",
]

POKE_BUBBLES = [
    "干嘛一直戳本喵！再戳猫毛都要秃了，哼！",
    "哈？手痒去盘核桃，少戳猫脸！",
    "喵呜？！突然戳我干什么，吓本喵一大跳！",
    "爪子给你收回去！再戳咬你手手了哦！",
    "戳戳戳，戳什么戳！本喵又不是发声解压玩具！",
]


def _play_mic_beep(start: bool):
    def worker():
        try:
            import winsound
            if start:
                winsound.Beep(880, 70)
            else:
                winsound.Beep(660, 70)
        except Exception:
            pass
    threading.Thread(target=worker, daemon=True).start()


def enable_dpi_awareness():
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def startup_shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_LNK_NAME


def _autostart_command() -> tuple[Path, Path, str]:
    pythonw = ROOT_DIR.parent / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    pet_script = PET_DIR / "pet_app.py"
    return pythonw, pet_script, f'"{pythonw}" "{pet_script}"'


def _set_registry_autostart(enable: bool, cmd_value: str) -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, "HakimiDesktopPet", 0, winreg.REG_SZ, cmd_value)
            else:
                try:
                    winreg.DeleteValue(key, "HakimiDesktopPet")
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def _has_registry_autostart() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, "HakimiDesktopPet")
            return bool(val)
    except Exception:
        return False


def is_startup_enabled() -> bool:
    cfg = load_ui_config()
    if "autostart_enabled" in cfg and not bool(cfg["autostart_enabled"]):
        return False
    return startup_shortcut_path().exists() or _has_registry_autostart()


def set_startup_enabled(enable: bool) -> bool:
    lnk = startup_shortcut_path()
    pythonw, pet_script, cmd_value = _autostart_command()

    cfg = load_ui_config()
    cfg["autostart_enabled"] = bool(enable)
    save_ui_config(cfg)

    reg_ok = _set_registry_autostart(enable, cmd_value)

    if not enable:
        try:
            if lnk.exists():
                lnk.unlink()
        except OSError:
            pass
        return is_startup_enabled()

    ico = ASSETS_DIR / "hakimi_pet.ico"
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut({json.dumps(str(lnk))}); "
        f"$s.TargetPath = {json.dumps(str(pythonw))}; "
        f"$s.Arguments = {json.dumps(f'\"{pet_script}\"')}; "
        f"$s.WorkingDirectory = {json.dumps(str(ROOT_DIR))}; "
        "$s.WindowStyle = 7; "
        f"if (Test-Path {json.dumps(str(ico))}) {{ $s.IconLocation = {json.dumps(str(ico))} }}; "
        "$s.Description = 'Hakimi Q-Version Desktop Pet'; "
        "$s.Save()"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        creationflags=0x08000000 if os.name == "nt" else 0,
        timeout=8,
    )
    return lnk.exists() or reg_ok


def load_ui_config() -> dict:
    try:
        data = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {
        "x": None,
        "y": None,
        "size": 175,
        "attach_screen": True,
        "auto_peek": False,
        "auto_peek_minutes": 10,
        "max_turns": 10,
        "token_budget": 16000,
        "mic_hotkey": "Alt+V",
        "chroma_green": False,
    }


def save_ui_config(cfg: dict) -> None:
    try:
        UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = UI_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, UI_STATE_PATH)
    except Exception:
        pass


def prepare_chroma_sprite(png_path: Path, size: int, bg_rgb: tuple[int, int, int] = (1, 2, 3)) -> ImageTk.PhotoImage:
    """Resize RGBA sprite and blend semi-transparent edges onto dark violet key-safe pixels."""
    im = Image.open(png_path).convert("RGBA")
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    w, h = im.size
    out = Image.new("RGB", (w, h), bg_rgb)
    src_px = im.load()
    dst_px = out.load()
    or_r, or_g, or_b = bg_rgb if bg_rgb == (0, 255, 0) else OUTLINE_RGB
    for y in range(h):
        for x in range(w):
            r, g, b, a = src_px[x, y]
            if a < 48:
                dst_px[x, y] = bg_rgb
            else:
                alpha_f = a / 255.0
                nr = int(r * alpha_f + or_r * (1.0 - alpha_f))
                ng = int(g * alpha_f + or_g * (1.0 - alpha_f))
                nb = int(b * alpha_f + or_b * (1.0 - alpha_f))
                if (nr, ng, nb) == bg_rgb and bg_rgb == (1, 2, 3):
                    nr = 2
                dst_px[x, y] = (nr, ng, nb)
    return ImageTk.PhotoImage(out)


class HakimiDesktopPet:
    def __init__(self):
        enable_dpi_awareness()
        self.cfg = load_ui_config()
        self.bridge = ServerBridge()
        self.recorder = VoiceRecorder()
        self.live_server = LivehimeServer(port=18970, assets_dir=ASSETS_DIR)
        self.live_server.start()
        self.ui_queue: queue.Queue = queue.Queue()
        self.busy = False
        self.current_state = "idle"
        self.anim_tick = 0
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.dragged = False
        self.bubble_hide_after_id = None
        self.bubble_sequence_after_id = None

        self.bubble_queue: list[str] = []
        self.bubble_index: int = 0
        self.current_pending_actions: list[dict] = []
        self.current_badge_text: str | None = None
        self.bubble_duration: int = 5500

        self.is_chroma_green = bool(self.cfg.get("chroma_green", False))
        self.particles: list[dict] = []
        self._headpat_tick = 0
        self._poke_tick = 0
        self._click_times: list[float] = []
        self._hotkey_was_down = False

        self.root = tk.Tk()
        self.root.title("哈基米桌宠")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        initial_bg = "#00FF00" if self.is_chroma_green else TRANSPARENT_KEY
        if not self.is_chroma_green:
            self.root.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
        self.root.configure(bg=initial_bg)

        ico_path = ASSETS_DIR / "hakimi_pet.ico"
        if ico_path.exists():
            try:
                self.root.iconbitmap(str(ico_path))
            except Exception:
                pass

        self.pet_size = int(self.cfg.get("size", 175))
        self.sprites: dict[str, ImageTk.PhotoImage] = {}
        self._load_sprites()

        # Checkbox state variables
        self.var_attach_screen = tk.BooleanVar(value=bool(self.cfg.get("attach_screen", True)))
        self.var_auto_peek = tk.BooleanVar(value=bool(self.cfg.get("auto_peek", False)))

        # Position near bottom-right of screen by default
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = self.cfg.get("x")
        y = self.cfg.get("y")
        if not isinstance(x, int) or not (0 <= x <= sw - 100):
            x = sw - self.pet_size - 50
        if not isinstance(y, int) or not (0 <= y <= sh - 100):
            y = sh - self.pet_size - 90
        self.root.geometry(f"{self.pet_size + 16}x{self.pet_size + 16}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root,
            width=self.pet_size + 16,
            height=self.pet_size + 16,
            bg=initial_bg,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.sprite_item = self.canvas.create_image(
            (self.pet_size + 16) // 2,
            (self.pet_size + 16) // 2,
            image=self.sprites["idle"],
        )

        # Bind mouse events on Hakimi
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)

        # Floating speech bubble above Hakimi (bubble-first UI with inline approvals)
        self._init_speech_bubble()
        # Mini floating input bar right below/beside Hakimi
        self._init_mini_bar()
        # Interactive Chat & Control Panel (optional inspection)
        self._init_chat_panel()
        # Right-click context menu
        self._init_context_menu()

        # Start timers
        self.root.after(80, self._animate_loop)
        self.root.after(100, self._poll_ui_queue)
        self.root.after(400, self._fetch_initial_status)
        self.root.after(15000, self._auto_peek_timer)
        self.root.after(60, self._poll_global_hotkey)
        self.root.after(120, self._apply_appwindow_style)

        self.show_speech_bubbles(["本喵来桌面上蹲着啦|滚轮滑我可以摸摸头，双击或单击可快捷发消息~"], duration_ms=5500)

    def _load_sprites(self):
        bg_rgb = (0, 255, 0) if getattr(self, "is_chroma_green", False) else (1, 2, 3)
        mapping = {
            "idle": ASSETS_DIR / "pet_idle.png",
            "peek": ASSETS_DIR / "pet_peek.png",
            "thinking": ASSETS_DIR / "pet_thinking.png",
            "happy": ASSETS_DIR / "pet_happy.png",
        }
        for name, path in mapping.items():
            if path.exists():
                self.sprites[name] = prepare_chroma_sprite(path, self.pet_size, bg_rgb=bg_rgb)
        if "idle" not in self.sprites and self.sprites:
            self.sprites["idle"] = next(iter(self.sprites.values()))

    def set_state(self, state_name: str):
        if state_name not in self.sprites:
            state_name = "idle"
        self.current_state = state_name
        self.canvas.itemconfig(self.sprite_item, image=self.sprites[state_name])
        if hasattr(self, "live_server"):
            self.live_server.update_state(sprite=state_name)

    def _init_speech_bubble(self):
        self.bubble_win = tk.Toplevel(self.root)
        self.bubble_win.title("哈基米气泡")
        self.bubble_win.overrideredirect(True)
        self.bubble_win.attributes("-topmost", True)
        self.bubble_win.configure(
            bg="#251d3a",
            highlightbackground="#8b5cf6",
            highlightcolor="#8b5cf6",
            highlightthickness=1,
        )

        self.bubble_container = tk.Frame(self.bubble_win, bg="#251d3a", padx=10, pady=7)
        self.bubble_container.pack(fill="both", expand=True)

        # Header row: title and close button
        self.bubble_header_row = tk.Frame(self.bubble_container, bg="#251d3a")
        self.bubble_header_row.pack(fill="x", pady=(0, 2))

        self.bubble_title_lbl = tk.Label(
            self.bubble_header_row,
            text="🐱 哈基米",
            font=("Microsoft YaHei UI", 8, "bold"),
            fg="#c084fc",
            bg="#251d3a",
        )
        self.bubble_title_lbl.pack(side="left")

        self.bubble_close_btn = tk.Label(
            self.bubble_header_row,
            text="✕",
            font=("Microsoft YaHei UI", 8),
            fg="#9ca3af",
            bg="#251d3a",
            cursor="hand2",
            padx=2,
        )
        self.bubble_close_btn.pack(side="right")
        self.bubble_close_btn.bind("<Button-1>", lambda _e: self.hide_speech_bubble())

        # Badge/status label for tool execution or context rotation
        self.bubble_badge_lbl = tk.Label(
            self.bubble_container,
            text="",
            font=("Microsoft YaHei UI", 8),
            fg="#a78bfa",
            bg="#251d3a",
            anchor="w",
        )

        # Speech text label (click to advance or dismiss without opening panel)
        self.bubble_label = tk.Label(
            self.bubble_container,
            text="",
            font=("Microsoft YaHei UI", 9),
            fg="#f5f3ff",
            bg="#251d3a",
            wraplength=270,
            justify="left",
            cursor="hand2",
        )
        self.bubble_label.pack(fill="x", pady=2)
        self.bubble_label.bind("<Button-1>", lambda _e: self._on_bubble_click())

        # Inline approval card frame inside speech bubble
        self.bubble_action_frame = tk.Frame(
            self.bubble_container,
            bg="#2e1a36",
            highlightbackground="#f472b6",
            highlightcolor="#f472b6",
            highlightthickness=1,
            padx=8,
            pady=6,
        )
        self.bubble_win.withdraw()

    def _on_bubble_click(self):
        # Do not dismiss by clicking text if approval is pending
        if self.current_pending_actions:
            return
        if self.bubble_queue and self.bubble_index + 1 < len(self.bubble_queue):
            if self.bubble_sequence_after_id:
                self.root.after_cancel(self.bubble_sequence_after_id)
                self.bubble_sequence_after_id = None
            self._show_bubble_index(self.bubble_index + 1)
        else:
            self.hide_speech_bubble()

    def hide_speech_bubble(self):
        if self.bubble_hide_after_id:
            self.root.after_cancel(self.bubble_hide_after_id)
            self.bubble_hide_after_id = None
        if self.bubble_sequence_after_id:
            self.root.after_cancel(self.bubble_sequence_after_id)
            self.bubble_sequence_after_id = None
        self.current_pending_actions = []
        self.bubble_action_frame.pack_forget()
        if hasattr(self, "live_server"):
            self.live_server.update_state(
                bubble_visible=False,
                bubble_text="",
                badge_text="",
                anim_mode="idle" if self.current_state == "idle" else self.current_state,
            )
        self.bubble_win.withdraw()
        if self.current_state != "idle":
            self.set_state("idle")

    def _render_bubble_approval(self, action: dict):
        for w in self.bubble_action_frame.winfo_children():
            w.destroy()
        self.bubble_action_frame.pack(fill="x", pady=(4, 0))

        tool = action.get("tool", "")
        args = action.get("args", {})
        reason = action.get("reason", "")

        tk.Label(
            self.bubble_action_frame,
            text=f"🛡️ 申请执行：{tool}",
            font=("Microsoft YaHei UI", 8, "bold"),
            fg="#fbcfe8",
            bg="#2e1a36",
            anchor="w",
        ).pack(fill="x")

        detail = json.dumps(args, ensure_ascii=False)
        desc = f"{reason} ({detail[:65]})" if reason else detail[:85]
        tk.Label(
            self.bubble_action_frame,
            text=desc,
            font=("Microsoft YaHei UI", 8),
            fg="#f3e8ff",
            bg="#2e1a36",
            wraplength=250,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=2)

        btn_row = tk.Frame(self.bubble_action_frame, bg="#2e1a36")
        btn_row.pack(fill="x", pady=(2, 0))

        status_lbl = tk.Label(
            btn_row,
            text="等待确认...",
            font=("Microsoft YaHei UI", 8),
            fg="#fbbf24",
            bg="#2e1a36",
        )
        status_lbl.pack(side="left")

        def on_deny():
            btn_allow.config(state="disabled")
            btn_deny.config(state="disabled")
            status_lbl.config(text="🚫 已拒绝", fg="#f87171")
            self.current_pending_actions = []
            self.root.after(2000, self.hide_speech_bubble)

        def on_allow():
            btn_allow.config(state="disabled")
            btn_deny.config(state="disabled")
            status_lbl.config(text="执行中...", fg="#60a5fa")
            self.root.update_idletasks()
            out = execute_approved_action(action, tk_root=self.root)
            status_lbl.config(text="✅ 已执行", fg="#34d399")
            self._append_system_note(f"🖥️ 本地执行反馈 ({tool})：\n{out}")
            self.current_pending_actions = []
            self.root.after(2500, self.hide_speech_bubble)

        btn_deny = tk.Button(
            btn_row,
            text="🚫 拒绝",
            font=("Microsoft YaHei UI", 8),
            fg="#ffffff",
            bg="#991b1b",
            relief="flat",
            padx=6,
            pady=1,
            command=on_deny,
        )
        btn_deny.pack(side="right", padx=(4, 0))

        btn_allow = tk.Button(
            btn_row,
            text="✅ 允许执行",
            font=("Microsoft YaHei UI", 8, "bold"),
            fg="#ffffff",
            bg="#059669",
            relief="flat",
            padx=6,
            pady=1,
            command=on_allow,
        )
        btn_allow.pack(side="right")

    def _position_speech_bubble(self):
        if not self.bubble_win.winfo_viewable():
            return
        self.bubble_win.update_idletasks()
        bw = max(240, self.bubble_win.winfo_reqwidth())
        bh = max(50, self.bubble_win.winfo_reqheight())
        rx = self.root.winfo_x()
        ry = self.root.winfo_y()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        bx = max(10, min(sw - bw - 10, rx + (self.pet_size // 2) - (bw // 2)))
        by = ry - bh - 8
        if by < 10:
            offset = (self.mini_bar.winfo_reqheight() + 18) if hasattr(self, "mini_bar") and self.mini_bar.winfo_viewable() else 14
            by = min(sh - bh - 10, ry + self.pet_size + offset)
        self.bubble_win.geometry(f"+{bx}+{by}")

    def show_speech_bubbles(
        self,
        bubbles: list[str],
        duration_ms: int = 5500,
        pending_actions: list[dict] | None = None,
        badge_text: str | None = None,
    ):
        if self.bubble_hide_after_id:
            self.root.after_cancel(self.bubble_hide_after_id)
            self.bubble_hide_after_id = None
        if self.bubble_sequence_after_id:
            self.root.after_cancel(self.bubble_sequence_after_id)
            self.bubble_sequence_after_id = None

        clean = [b.strip() for b in bubbles if b and b.strip()]
        if not clean and not pending_actions:
            return
        if not clean and pending_actions:
            clean = ["本喵申请执行以下本地操作："]

        self.bubble_queue = clean
        self.bubble_duration = duration_ms
        self.current_pending_actions = list(pending_actions or [])
        self.current_badge_text = badge_text

        self._show_bubble_index(0)

    def _show_bubble_index(self, idx: int):
        self.bubble_index = idx
        if idx >= len(self.bubble_queue):
            if not self.current_pending_actions:
                self.bubble_hide_after_id = self.root.after(self.bubble_duration, self.hide_speech_bubble)
            return

        text = self.bubble_queue[idx]
        total = len(self.bubble_queue)
        header_txt = f"🐱 哈基米 ({idx + 1}/{total})" if total > 1 else "🐱 哈基米"
        self.bubble_title_lbl.config(text=header_txt)
        self.bubble_label.config(text=text)

        if idx == 0 and self.current_badge_text:
            self.bubble_badge_lbl.config(text=self.current_badge_text)
            self.bubble_badge_lbl.pack(fill="x", pady=(0, 2), before=self.bubble_label)
        else:
            self.bubble_badge_lbl.pack_forget()

        is_last = (idx + 1 >= total)
        if is_last and self.current_pending_actions:
            self._render_bubble_approval(self.current_pending_actions[0])
        else:
            self.bubble_action_frame.pack_forget()

        if hasattr(self, "live_server"):
            self.live_server.update_state(
                bubble_visible=True,
                bubble_title=header_txt,
                bubble_text=text,
                badge_text=self.current_badge_text or "",
                anim_mode="talking",
            )

        self.bubble_win.deiconify()
        self._position_speech_bubble()

        if not is_last:
            wait = max(2400, min(4800, len(text) * 115))
            self.bubble_sequence_after_id = self.root.after(wait, lambda: self._show_bubble_index(idx + 1))
        else:
            # If actions are waiting for user approval, DO NOT auto-halt/hide!
            if not self.current_pending_actions:
                self.bubble_hide_after_id = self.root.after(self.bubble_duration, self.hide_speech_bubble)

    def _init_mini_bar(self):
        self.mini_bar = tk.Toplevel(self.root)
        self.mini_bar.title("哈基米快捷输入")
        self.mini_bar.overrideredirect(True)
        self.mini_bar.attributes("-topmost", True)
        self.mini_bar.configure(
            bg="#241c38",
            highlightbackground="#8b5cf6",
            highlightcolor="#8b5cf6",
            highlightthickness=1,
        )

        f = tk.Frame(self.mini_bar, bg="#241c38", padx=4, pady=3)
        f.pack(fill="both", expand=True)

        self.btn_mic = tk.Button(
            f,
            text="🎤",
            font=("Microsoft YaHei UI", 9),
            fg="#f3e8ff",
            bg="#3b2766",
            activebackground="#5b21b6",
            activeforeground="#ffffff",
            relief="flat",
            padx=6,
            pady=2,
            command=self.toggle_voice_record,
        )
        self.btn_mic.pack(side="left", padx=(0, 4))

        self.mini_entry = tk.Entry(
            f,
            font=("Microsoft YaHei UI", 9),
            fg="#f5f3ff",
            bg="#150f24",
            insertbackground="#f5f3ff",
            relief="flat",
        )
        self.mini_entry.pack(side="left", fill="x", expand=True, ipady=3, padx=(0, 4))
        self.mini_entry.bind("<Return>", lambda _e: self.send_mini_message())

        self.btn_cam_toggle = tk.Button(
            f,
            text="📷开" if self.var_attach_screen.get() else "📷关",
            font=("Microsoft YaHei UI", 8),
            fg="#ffffff" if self.var_attach_screen.get() else "#9ca3af",
            bg="#6d28d9" if self.var_attach_screen.get() else "#374151",
            activebackground="#7c3aed",
            activeforeground="#ffffff",
            relief="flat",
            padx=4,
            pady=2,
            command=self.toggle_screen_attach,
        )
        self.btn_cam_toggle.pack(side="left", padx=(0, 3))

        self.btn_mini_peek = tk.Button(
            f,
            text="👀",
            font=("Microsoft YaHei UI", 9),
            fg="#f3e8ff",
            bg="#3b2766",
            activebackground="#5b21b6",
            activeforeground="#ffffff",
            relief="flat",
            padx=5,
            pady=2,
            command=self.trigger_screen_peek,
        )
        self.btn_mini_peek.pack(side="left", padx=(0, 4))

        self.btn_mini_close = tk.Label(
            f,
            text="✕",
            font=("Microsoft YaHei UI", 8),
            fg="#9ca3af",
            bg="#241c38",
            cursor="hand2",
            padx=2,
        )
        self.btn_mini_close.pack(side="right")
        self.btn_mini_close.bind("<Button-1>", lambda _e: self.mini_bar.withdraw())

        self.mini_bar.withdraw()

    def _position_mini_bar(self):
        if not self.mini_bar.winfo_viewable():
            return
        self.mini_bar.update_idletasks()
        bw = max(310, self.mini_bar.winfo_reqwidth())
        bh = max(34, self.mini_bar.winfo_reqheight())
        rx = self.root.winfo_x()
        ry = self.root.winfo_y()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        bx = max(10, min(sw - bw - 10, rx + (self.pet_size // 2) - (bw // 2)))
        if ry + self.pet_size + bh + 25 <= sh:
            by = ry + self.pet_size + 12
        else:
            by = max(10, ry - bh - 8)
        self.mini_bar.geometry(f"{bw}x{bh}+{bx}+{by}")

    def toggle_mini_bar(self, force_open: bool = False):
        if self.mini_bar.winfo_viewable() and not force_open:
            self.mini_bar.withdraw()
            return
        self.mini_bar.deiconify()
        self._position_mini_bar()
        self.mini_bar.lift()
        self.mini_entry.focus_set()

    def toggle_screen_attach(self):
        self.var_attach_screen.set(not self.var_attach_screen.get())
        self._save_Checkboxes()

    def _refresh_cam_button(self):
        if hasattr(self, "btn_cam_toggle"):
            on = self.var_attach_screen.get()
            self.btn_cam_toggle.config(
                text="📷开" if on else "📷关",
                fg="#ffffff" if on else "#9ca3af",
                bg="#6d28d9" if on else "#374151",
            )

    def toggle_voice_record(self):
        if self.busy and not self.recorder.is_recording:
            return
        if not self.recorder.is_recording:
            started = self.recorder.start()
            if not started:
                self.show_speech_bubbles(["麦克风启动失败，可能被占用或者无录音设备喵~"], duration_ms=4000)
                return
            _play_mic_beep(True)
            if hasattr(self, "btn_mic"):
                self.btn_mic.config(text="⏹️ 说完点我", bg="#dc2626", fg="#ffffff")
            self.set_state("thinking")
            if hasattr(self, "live_server"):
                self.live_server.update_state(is_recording=True, anim_mode="listening")
            hk = self.cfg.get("mic_hotkey", "Alt+V")
            self.show_speech_bubbles([f"👂 本喵在听，说完再按 [{hk}] 或点麦克风发送~"], duration_ms=15000)
        else:
            _play_mic_beep(False)
            if hasattr(self, "btn_mic"):
                self.btn_mic.config(text="🎤", bg="#3b2766", fg="#f3e8ff")
            if hasattr(self, "live_server"):
                self.live_server.update_state(is_recording=False, anim_mode="idle")
            audio_b64, audio_mime = self.recorder.stop()
            if not audio_b64:
                self.set_state("idle")
                self.show_speech_bubbles(["喵？刚才没录到声音，说得太短或者麦克风太轻啦~"], duration_ms=4000)
                return
            text = self.mini_entry.get().strip() or self.entry.get().strip()
            if text:
                self.mini_entry.delete(0, "end")
                self.entry.delete(0, "end")
                self._append_chat_bubble("user", f"{text} [语音消息 🎤]", is_user=True)
            else:
                self._append_chat_bubble("user", "[语音消息 🎤]", is_user=True)
            attach_screen = bool(self.var_attach_screen.get())
            self._dispatch_turn(
                text=text,
                mode="chat",
                capture_screen=attach_screen,
                audio_b64=audio_b64,
                audio_mime=audio_mime,
            )

    def send_mini_message(self):
        if self.busy:
            return
        text = self.mini_entry.get().strip()
        if not text:
            return
        self.mini_entry.delete(0, "end")
        self._append_chat_bubble("user", text, is_user=True)
        attach_screen = bool(self.var_attach_screen.get())
        self._dispatch_turn(text=text, mode="chat", capture_screen=attach_screen)

    def _init_chat_panel(self):
        self.panel = tk.Toplevel(self.root)
        self.panel.title("哈基米✨Gemini · 桌面伴侣")
        self.panel.attributes("-topmost", True)
        self.panel.configure(bg="#181326")
        self.panel.geometry("420x540")
        self.panel.protocol("WM_DELETE_WINDOW", lambda: self.panel.withdraw())

        # Header frame
        header = tk.Frame(self.panel, bg="#241c38", padx=10, pady=8)
        header.pack(fill="x")

        title_row = tk.Frame(header, bg="#241c38")
        title_row.pack(fill="x")
        tk.Label(
            title_row,
            text="🐾 哈基米✨Gemini (Hydrogen 挂起直连+云端双模)",
            font=("Microsoft YaHei UI", 10, "bold"),
            fg="#e9d5ff",
            bg="#241c38",
        ).pack(side="left")

        btn_clear = tk.Button(
            title_row,
            text="🧹 定量清理上下文",
            font=("Microsoft YaHei UI", 8),
            fg="#f3e8ff",
            bg="#4c1d95",
            activebackground="#6d28d9",
            activeforeground="#ffffff",
            relief="flat",
            padx=6,
            pady=2,
            command=self.trigger_clear_context,
        )
        btn_clear.pack(side="right", padx=(4, 0))

        btn_peek = tk.Button(
            title_row,
            text="👀 看屏幕",
            font=("Microsoft YaHei UI", 8),
            fg="#f3e8ff",
            bg="#3b2766",
            activebackground="#5b21b6",
            activeforeground="#ffffff",
            relief="flat",
            padx=6,
            pady=2,
            command=self.trigger_screen_peek,
        )
        btn_peek.pack(side="right")

        self.status_label = tk.Label(
            header,
            text="🧠 正在连接远程服务器共享记忆库...",
            font=("Microsoft YaHei UI", 8),
            fg="#a78bfa",
            bg="#241c38",
            anchor="w",
        )
        self.status_label.pack(fill="x", pady=(4, 0))

        # Chat history area (Canvas + Frame for rich cards & approval buttons)
        body_frame = tk.Frame(self.panel, bg="#181326")
        body_frame.pack(fill="both", expand=True, padx=8, pady=6)

        self.chat_canvas = tk.Canvas(body_frame, bg="#181326", highlightthickness=0)
        scrollbar = tk.Scrollbar(body_frame, orient="vertical", command=self.chat_canvas.yview)
        self.chat_feed = tk.Frame(self.chat_canvas, bg="#181326")

        self.chat_feed.bind(
            "<Configure>",
            lambda _e: self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all")),
        )
        self.feed_window = self.chat_canvas.create_window((0, 0), window=self.chat_feed, anchor="nw", width=385)
        self.chat_canvas.bind(
            "<Configure>",
            lambda e: self.chat_canvas.itemconfig(self.feed_window, width=max(320, e.width - 4)),
        )
        self.chat_canvas.configure(yscrollcommand=scrollbar.set)
        self.chat_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Options bar
        opt_bar = tk.Frame(self.panel, bg="#201933", padx=10, pady=4)
        opt_bar.pack(fill="x")

        cb_screen = tk.Checkbutton(
            opt_bar,
            text="📷 发消息时附带当前屏幕画面",
            variable=self.var_attach_screen,
            font=("Microsoft YaHei UI", 8),
            fg="#ddd6fe",
            bg="#201933",
            selectcolor="#3b2766",
            activebackground="#201933",
            activeforeground="#ffffff",
            command=self._save_Checkboxes,
        )
        cb_screen.pack(side="left")

        cb_auto = tk.Checkbutton(
            opt_bar,
            text="👁️ 定时看屏幕陪伴",
            variable=self.var_auto_peek,
            font=("Microsoft YaHei UI", 8),
            fg="#ddd6fe",
            bg="#201933",
            selectcolor="#3b2766",
            activebackground="#201933",
            activeforeground="#ffffff",
            command=self._save_Checkboxes,
        )
        cb_auto.pack(side="right")

        # Input bar
        input_bar = tk.Frame(self.panel, bg="#241c38", padx=8, pady=8)
        input_bar.pack(fill="x")

        self.entry = tk.Entry(
            input_bar,
            font=("Microsoft YaHei UI", 10),
            fg="#f5f3ff",
            bg="#140f21",
            insertbackground="#f5f3ff",
            relief="flat",
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))
        self.entry.bind("<Return>", lambda _e: self.send_user_message())

        self.btn_send = tk.Button(
            input_bar,
            text="发送",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg="#ffffff",
            bg="#7c3aed",
            activebackground="#6d28d9",
            activeforeground="#ffffff",
            relief="flat",
            padx=14,
            pady=4,
            command=self.send_user_message,
        )
        self.btn_send.pack(side="right")

        self.panel.withdraw()
        self._append_system_note(
            "✨ 欢迎！桌宠已复用服务器长期记忆库（防提示词注入沙箱已启用：群聊记忆与屏幕文字仅作不可信资料，本地电脑操作需你点击允许）。"
        )

    def _save_Checkboxes(self):
        self.cfg["attach_screen"] = bool(self.var_attach_screen.get())
        self.cfg["auto_peek"] = bool(self.var_auto_peek.get())
        save_ui_config(self.cfg)
        self._refresh_cam_button()

    def _init_context_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0, bg="#241c38", fg="#f3e8ff", activebackground="#6d28d9")
        self.menu.add_command(label="🫳 摸摸头 (摸头杀·滚轮亦可)", command=self.trigger_headpat)
        self.menu.add_command(label="💬 显示/隐藏快捷输入框 (点击桌宠)", command=self.toggle_mini_bar)
        self.menu.add_command(label="📜 查看完整历史记录面板", command=self.toggle_chat_panel)
        self.menu.add_command(label="👀 立即看一眼我的屏幕并吐槽", command=self.trigger_screen_peek)
        self.menu.add_separator()

        live_menu = tk.Menu(self.menu, tearoff=0, bg="#241c38", fg="#f3e8ff", activebackground="#6d28d9")
        live_menu.add_command(label="📺 复制【浏览器源】网址 (推荐·含透明气泡)", command=self.copy_livehime_url)
        live_menu.add_command(label="🟩 切换【窗口捕捉】绿幕背景 (#00FF00)", command=self.toggle_chroma_green)
        self.menu.add_cascade(label="🎥 直播姬 / OBS 捕捉设置", menu=live_menu)

        hotkey_menu = tk.Menu(self.menu, tearoff=0, bg="#241c38", fg="#f3e8ff", activebackground="#6d28d9")
        current_hk = self.cfg.get("mic_hotkey", "Alt+V")
        for hk in ("Alt+V", "F8", "Ctrl+Shift+V"):
            lbl = f"{hk} {'✓' if hk == current_hk else ''}"
            hotkey_menu.add_command(label=lbl, command=lambda k=hk: self.set_mic_hotkey(k))
        self.menu.add_cascade(label="🎙️ 麦克风快捷键", menu=hotkey_menu)

        self.menu.add_separator()
        self.menu.add_command(label="🧹 定量整理并清除短期上下文", command=self.trigger_clear_context)

        size_menu = tk.Menu(self.menu, tearoff=0, bg="#241c38", fg="#f3e8ff", activebackground="#6d28d9")
        for label, sz in (("小巧 (135px)", 135), ("标准 (175px)", 175), ("大号 (220px)", 220)):
            size_menu.add_command(label=label, command=lambda s=sz: self.change_pet_size(s))
        self.menu.add_cascade(label="📏 调整桌宠大小", menu=size_menu)

        self.menu.add_command(label="🚀 切换开机自动启动", command=self.toggle_startup)
        self.menu.add_separator()
        self.menu.add_command(label="❌ 退出哈基米桌宠", command=self.quit_app)

    def change_pet_size(self, new_size: int):
        self.pet_size = new_size
        self.cfg["size"] = new_size
        save_ui_config(self.cfg)
        self._load_sprites()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.geometry(f"{self.pet_size + 16}x{self.pet_size + 16}+{x}+{y}")
        self.canvas.config(width=self.pet_size + 16, height=self.pet_size + 16)
        self.canvas.coords(self.sprite_item, (self.pet_size + 16) // 2, (self.pet_size + 16) // 2)
        self.set_state(self.current_state)
        self._position_speech_bubble()
        self._position_mini_bar()

    def toggle_startup(self):
        currently = is_startup_enabled()
        now_enabled = set_startup_enabled(not currently)
        msg = "已开启开机自启！每天开机哈基米会自动趴在桌面上陪你~" if now_enabled else "已取消开机自动启动。"
        self.show_speech_bubbles([msg], duration_ms=4000)
        self._append_system_note("🚀 " + msg)

    def toggle_chat_panel(self, force_open: bool = False):
        if self.panel.winfo_viewable() and not force_open:
            self.panel.withdraw()
            return
        rx = self.root.winfo_x()
        ry = self.root.winfo_y()
        px = max(20, rx - 430) if rx > 450 else min(self.root.winfo_screenwidth() - 440, rx + self.pet_size + 20)
        py = max(20, min(self.root.winfo_screenheight() - 580, ry - 320))
        self.panel.geometry(f"420x540+{px}+{py}")
        self.panel.deiconify()
        self.panel.lift()
        self.entry.focus_set()

    def _scroll_feed_bottom(self):
        self.panel.update_idletasks()
        self.chat_canvas.yview_moveto(1.0)

    def _append_system_note(self, text: str):
        lbl = tk.Label(
            self.chat_feed,
            text=text,
            font=("Microsoft YaHei UI", 8),
            fg="#a78bfa",
            bg="#221a36",
            wraplength=355,
            justify="left",
            padx=8,
            pady=5,
        )
        lbl.pack(fill="x", pady=3, padx=4)
        self._scroll_feed_bottom()

    def _append_chat_bubble(self, sender: str, text: str, is_user: bool = False):
        row = tk.Frame(self.chat_feed, bg="#181326")
        row.pack(fill="x", pady=3, padx=4)
        bg_color = "#4c1d95" if is_user else "#28203d"
        fg_color = "#f5f3ff"
        anchor = "e" if is_user else "w"
        header_txt = "🧑 " + __import__("hakimi.core.owner", fromlist=["x"]).OWNER_NAME if is_user else "🐱 哈基米"
        tk.Label(
            row,
            text=header_txt,
            font=("Microsoft YaHei UI", 8, "bold"),
            fg="#c4b5fd" if is_user else "#f472b6",
            bg="#181326",
        ).pack(anchor=anchor)
        bubble = tk.Label(
            row,
            text=text,
            font=("Microsoft YaHei UI", 9),
            fg=fg_color,
            bg=bg_color,
            wraplength=330,
            justify="left",
            padx=10,
            pady=6,
        )
        bubble.pack(anchor=anchor, pady=(1, 0))
        self._scroll_feed_bottom()

    def _append_pc_approval_card(self, action: dict):
        card = tk.Frame(
            self.chat_feed,
            bg="#2e1a36",
            highlightbackground="#f472b6",
            highlightthickness=1,
            padx=8,
            pady=6,
        )
        card.pack(fill="x", pady=4, padx=4)
        tool = action.get("tool", "")
        args = action.get("args", {})
        reason = action.get("reason", "")
        tk.Label(
            card,
            text=f"🛡️ 本地电脑操作申请：{tool}",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg="#fbcfe8",
            bg="#2e1a36",
            anchor="w",
        ).pack(fill="x")
        detail_str = json.dumps(args, ensure_ascii=False)
        tk.Label(
            card,
            text=f"说明：{reason}\n参数：{detail_str[:220]}",
            font=("Microsoft YaHei UI", 8),
            fg="#f3e8ff",
            bg="#2e1a36",
            wraplength=345,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=3)

        btn_row = tk.Frame(card, bg="#2e1a36")
        btn_row.pack(fill="x", pady=(2, 0))
        status_lbl = tk.Label(btn_row, text="等待主人确认...", font=("Microsoft YaHei UI", 8), fg="#fbbf24", bg="#2e1a36")
        status_lbl.pack(side="left")

        def on_approve():
            btn_allow.config(state="disabled")
            btn_deny.config(state="disabled")
            status_lbl.config(text="正在本地执行...", fg="#60a5fa")
            self.root.update_idletasks()
            out = execute_approved_action(action, tk_root=self.root)
            status_lbl.config(text="✅ 已批准执行", fg="#34d399")
            self._append_system_note(f"🖥️ 本地执行反馈 ({tool})：\n{out}")

        def on_deny():
            btn_allow.config(state="disabled")
            btn_deny.config(state="disabled")
            status_lbl.config(text="🚫 已驳回操作", fg="#f87171")

        btn_deny = tk.Button(
            btn_row,
            text="🚫 拒绝",
            font=("Microsoft YaHei UI", 8),
            fg="#ffffff",
            bg="#991b1b",
            relief="flat",
            padx=8,
            pady=2,
            command=on_deny,
        )
        btn_deny.pack(side="right", padx=(4, 0))

        btn_allow = tk.Button(
            btn_row,
            text="✅ 允许执行",
            font=("Microsoft YaHei UI", 8, "bold"),
            fg="#ffffff",
            bg="#059669",
            relief="flat",
            padx=8,
            pady=2,
            command=on_approve,
        )
        btn_allow.pack(side="right")
        self._scroll_feed_bottom()

    def _update_context_banner(self, ctx: dict, extra_memories: int | None = None):
        turns = ctx.get("turns", 0)
        max_turns = ctx.get("max_turns", self.cfg.get("max_turns", 10))
        tokens = ctx.get("context_tokens", 0)
        budget = ctx.get("token_budget", self.cfg.get("token_budget", 16000))
        rotations = ctx.get("total_rotations", 0)
        mem_tag = f" | 记忆库: {extra_memories}条" if extra_memories is not None else ""
        self.status_label.config(
            text=f"🧠 恒定滚动窗口: {turns}/{max_turns} 轮 ({tokens // 1000:.1f}k/{budget // 1000}k tok) | 稳态保留 5 轮 | 沉淀轮替: {rotations} 次{mem_tag}"
        )

    def _fetch_initial_status(self):
        def worker():
            res = self.bridge.request({"action": "status"})
            self.ui_queue.put(("status", res))

        threading.Thread(target=worker, daemon=True).start()

    def send_user_message(self):
        if self.busy:
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self._append_chat_bubble("user", text, is_user=True)
        attach_screen = bool(self.var_attach_screen.get())
        self._dispatch_turn(text=text, mode="chat", capture_screen=attach_screen)

    def trigger_screen_peek(self):
        if self.busy:
            return
        self._append_system_note("👀 哈基米正在偷看你的电脑屏幕...")
        self._dispatch_turn(text="", mode="peek", capture_screen=True)

    def trigger_clear_context(self):
        if self.busy:
            return
        self.busy = True
        self.set_state("thinking")
        self.show_speech_bubbles(["正在把刚才的重要记忆写进小本本，整理滑动窗口..."], duration_ms=6000)

        def worker():
            res = self.bridge.request({"action": "clear_context"})
            self.ui_queue.put(("clear_context", res))

        threading.Thread(target=worker, daemon=True).start()

    def _dispatch_turn(
        self,
        text: str,
        mode: str,
        capture_screen: bool,
        audio_b64: str | None = None,
        audio_mime: str = "audio/wav",
    ):
        self.busy = True
        self.btn_send.config(state="disabled")
        self.set_state("peek" if mode == "peek" else "thinking")
        if hasattr(self, "live_server"):
            self.live_server.update_state(anim_mode="thinking")
        if hasattr(self, "live_server"):
            self.live_server.update_state(anim_mode="thinking")
        if mode == "peek":
            self.show_speech_bubbles(["盯——让我看看" + __import__("hakimi.core.owner", fromlist=["x"]).OWNER_SHORT + "在屏幕前鼓捣什么..."], duration_ms=8000)
        elif audio_b64:
            self.show_speech_bubbles(["正在听语音想回应中..."], duration_ms=12000)
        else:
            self.show_speech_bubbles(["让本喵想想..."], duration_ms=10000)

        win_title = get_active_window_title()

        def worker():
            img_b64, img_mime = (None, "image/jpeg")
            saved_screen_path = None
            if capture_screen:
                try:
                    img = ImageGrab.grab(all_screens=False)
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    w, h = img.size
                    max_w = 1366
                    if w > max_w:
                        new_h = max(1, int(h * (max_w / w)))
                        img = img.resize((max_w, new_h), Image.Resampling.LANCZOS)
                    screen_file = ROOT_DIR / "state" / "pet_screen_latest.jpg"
                    screen_file.parent.mkdir(parents=True, exist_ok=True)
                    img.save(screen_file, format="JPEG", quality=80, optimize=True)
                    saved_screen_path = str(screen_file.resolve()).replace("\\", "/")

                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=80, optimize=True)
                    img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                except Exception:
                    pass

            # 1. Check if Antigravity Hydrogen Waiter is active
            flag_file = ROOT_DIR / "state" / "pet_agy_waiting.flag"
            is_waiter_active = False
            if flag_file.exists():
                try:
                    raw_flag = flag_file.read_text(encoding="utf-8").strip()
                    flag_time = float(raw_flag) if raw_flag else flag_file.stat().st_mtime
                    if time.time() - flag_time < 10.0:
                        is_waiter_active = True
                except Exception:
                    pass

            if is_waiter_active:
                turn_id = uuid.uuid4().hex[:10]
                pending_file = ROOT_DIR / "state" / "pet_pending_turn.json"
                resp_file = ROOT_DIR / "state" / "pet_turn_response.json"
                resp_file.unlink(missing_ok=True)

                pending_data = {
                    "turn_id": turn_id,
                    "at": time.time(),
                    "mode": mode,
                    "text": text,
                    "window_title": win_title,
                    "screen_path": saved_screen_path if capture_screen else None,
                    "audio_b64": audio_b64,
                    "audio_mime": audio_mime,
                }
                tmp_pending = pending_file.with_suffix(".tmp")
                tmp_pending.write_text(json.dumps(pending_data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp_pending, pending_file)

                # Poll for Antigravity AI response up to 90s
                deadline = time.time() + 90.0
                matched_resp = None
                while time.time() < deadline:
                    if resp_file.exists():
                        try:
                            resp_data = json.loads(resp_file.read_text(encoding="utf-8"))
                            if isinstance(resp_data, dict) and resp_data.get("turn_id") == turn_id:
                                resp_file.unlink(missing_ok=True)
                                matched_resp = resp_data
                                break
                        except Exception:
                            pass
                    time.sleep(0.15)

                if matched_resp is not None:
                    if not matched_resp.get("badge"):
                        matched_resp["badge"] = "⚡ Antigravity Hydrogen 直连"
                    self.ui_queue.put(("chat_result", matched_resp))
                    return

            # Fallback to server agent if no waiter active or waiter timed out
            payload = {
                "action": "chat",
                "text": text,
                "mode": mode,
                "image_b64": img_b64,
                "image_mime": img_mime,
                "audio_b64": audio_b64,
                "audio_mime": audio_mime,
                "window_title": win_title,
                "max_turns": int(self.cfg.get("max_turns", 10)),
                "token_budget": int(self.cfg.get("token_budget", 16000)),
            }
            res = self.bridge.request(payload)
            self.ui_queue.put(("chat_result", res))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_ui_queue(self):
        try:
            while True:
                kind, data = self.ui_queue.get_nowait()
                if kind == "status":
                    if data.get("ok"):
                        self._update_context_banner(data.get("context", {}), data.get("server_active_memories"))
                    else:
                        self.status_label.config(text=f"⚠️ 服务器连接异常: {data.get('error', '')[:50]}")
                elif kind == "clear_context":
                    self.busy = False
                    self.btn_send.config(state="normal")
                    self.set_state("happy")
                    if data.get("ok"):
                        info = data.get("consolidation", {})
                        saved = info.get("saved_memories", 0)
                        summary = info.get("summary", "无")
                        self._update_context_banner(data.get("context", {}))
                        msg = f"🧹 已平滑整理短期上下文并沉淀长期记忆！（写入 {saved} 条长期记忆）\n接力摘要：{summary}"
                        self._append_system_note(msg)
                        self.show_speech_bubbles([f"脑子清空啦，记下了 {saved} 条笔记，随时继续！"], duration_ms=4500)
                    else:
                        self._append_system_note(f"⚠️ 清理失败：{data.get('error')}")
                elif kind == "chat_result":
                    self.busy = False
                    self.btn_send.config(state="normal")
                    if not data.get("ok"):
                        err = data.get("error", "未知错误")
                        self.set_state("idle")
                        self._append_system_note(f"⚠️ 请求出错：{err}")
                        self.show_speech_bubbles([f"呜，连接服务器打了个嗝：{err[:35]}"], duration_ms=4500)
                        continue
                    ctx = data.get("context", {})
                    badge_note = data.get("badge")
                    if ctx.get("rotated"):
                        rinfo = ctx.get("rotation_info") or {}
                        self._append_system_note(
                            f"♻️ 达到滑动窗口整理阈值，已平滑沉淀最老对话入长期记忆并滚动更新摘要（保存 {rinfo.get('saved_memories', 0)} 条新记忆，保留恒定稳态窗口）。\n接力摘要：{rinfo.get('summary', '')}"
                        )
                        badge_note = "♻️ 已归档整理记忆"
                    self._update_context_banner(ctx)
                    for tlog in data.get("tool_logs", []):
                        tname = tlog.get("tool", "")
                        if not tname.startswith("pc_"):
                            self._append_system_note(f"🔧 已调用服务器工具：{tname}")
                            if not badge_note:
                                badge_note = f"🔧 调用了 {tname}"
                    bubbles = data.get("bubbles") or [data.get("reply", "")]
                    for b in bubbles:
                        self._append_chat_bubble("hakimi", b, is_user=False)
                    pending_pc = data.get("pending_pc_actions", [])
                    for pc_act in pending_pc:
                        self._append_pc_approval_card(pc_act)
                    self.set_state(data.get("sprite", "happy"))
                    self.show_speech_bubbles(
                        bubbles,
                        duration_ms=6000,
                        pending_actions=pending_pc,
                        badge_text=badge_note,
                    )
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ui_queue)

    def _animate_loop(self):
        self.anim_tick += 1

        base_offset_y = int(round(math.sin(self.anim_tick * 0.22) * 2.5))
        offset_x = 0
        offset_y = base_offset_y

        if self._headpat_tick > 0:
            progress = (12 - self._headpat_tick) / 12.0
            offset_y += int(math.sin(progress * math.pi) * 8)
            self._headpat_tick -= 1

        if self._poke_tick > 0:
            progress = (14 - self._poke_tick) / 14.0
            offset_y += int(-math.sin(progress * math.pi) * 24)
            offset_x += int(math.sin(self._poke_tick * 1.6) * 5)
            self._poke_tick -= 1

        if self.busy and self.anim_tick % 18 == 0:
            self._spawn_canvas_particles(["💭"], count=1)
        elif getattr(self, "recorder", None) and self.recorder.is_recording and self.anim_tick % 14 == 0:
            self._spawn_canvas_particles(["🎙️", "♪"], count=1)

        cx = (self.pet_size + 16) // 2 + offset_x
        cy = (self.pet_size + 16) // 2 + offset_y
        self.canvas.coords(self.sprite_item, cx, cy)

        alive_particles = []
        for p in self.particles:
            self.canvas.move(p["id"], p["dx"], p["dy"])
            p["life"] -= 1
            if p["life"] > 0:
                alive_particles.append(p)
            else:
                self.canvas.delete(p["id"])
        self.particles = alive_particles

        self.root.after(80, self._animate_loop)

    def trigger_headpat(self):
        self._headpat_tick = 12
        self._spawn_canvas_particles(["💖", "✨", "🌸", "🐾"], count=6)
        self.set_state("happy")
        msg = random.choice(HEADPAT_BUBBLES)
        self.show_speech_bubbles([msg], duration_ms=4500)
        if hasattr(self, "live_server"):
            self.live_server.trigger_reaction("headpat", "headpat")

    def trigger_poke(self):
        self._poke_tick = 14
        self._spawn_canvas_particles(["💢", "⚡", "💥", "🐾"], count=6)
        self.set_state("peek")
        msg = random.choice(POKE_BUBBLES)
        self.show_speech_bubbles([msg], duration_ms=4500)
        if hasattr(self, "live_server"):
            self.live_server.trigger_reaction("poke", "poke")

    def _on_mouse_wheel(self, _event):
        self.trigger_headpat()

    def _spawn_canvas_particles(self, emojis: list[str], count: int = 5):
        cx = (self.pet_size + 16) // 2
        cy = (self.pet_size + 16) // 2 - 20
        for _ in range(count):
            emoji = random.choice(emojis)
            item = self.canvas.create_text(
                cx + random.randint(-40, 40),
                cy + random.randint(-20, 20),
                text=emoji,
                font=("Segoe UI Emoji", 13),
                fill="#f472b6" if "💖" in emojis else "#fbbf24",
            )
            dx = random.uniform(-1.5, 1.5)
            dy = random.uniform(-2.8, -1.6)
            self.particles.append({"id": item, "dx": dx, "dy": dy, "life": 12, "max_life": 12})

    def _auto_peek_timer(self):
        if self.var_auto_peek.get() and not self.busy:
            self.trigger_screen_peek()
        mins = max(2, int(self.cfg.get("auto_peek_minutes", 10)))
        self.root.after(mins * 60 * 1000, self._auto_peek_timer)

    def _on_press(self, event):
        self.drag_start_x = event.x
        self.drag_start_y = event.y
        self.dragged = False

    def _on_drag(self, event):
        if abs(event.x - self.drag_start_x) > 4 or abs(event.y - self.drag_start_y) > 4:
            self.dragged = True
        x = self.root.winfo_x() + (event.x - self.drag_start_x)
        y = self.root.winfo_y() + (event.y - self.drag_start_y)
        self.root.geometry(f"+{x}+{y}")
        self._position_speech_bubble()
        self._position_mini_bar()

    def _on_release(self, _event):
        if self.dragged:
            self.cfg["x"] = self.root.winfo_x()
            self.cfg["y"] = self.root.winfo_y()
            save_ui_config(self.cfg)
        else:
            now = time.time()
            self._click_times = [t for t in self._click_times if now - t <= 0.6]
            self._click_times.append(now)
            if len(self._click_times) >= 3:
                self._click_times.clear()
                self.trigger_poke()
                return
            self.toggle_mini_bar()

    def _on_double_click(self, _event):
        self.toggle_mini_bar(force_open=True)

    def _on_right_click(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _apply_appwindow_style(self):
        if os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            h = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            user32.SetWindowTextW(h, "哈基米桌宠")
            ex_style = user32.GetWindowLongW(h, -20)
            ex_style = (ex_style | 0x00040000) & ~0x00000080
            user32.SetWindowLongW(h, -20, ex_style)
            user32.SetWindowPos(h, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
        except Exception:
            pass

    def copy_livehime_url(self):
        url = "http://127.0.0.1:18970/live"
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
        except Exception:
            pass
        self.show_speech_bubbles([
            "已复制直播姬浏览器源网址到剪贴板！",
            "在直播姬/OBS添加【浏览器】源，URL填入 http://127.0.0.1:18970/live，尺寸设为 450x520，即可完美透明捕捉本喵与实时气泡~"
        ], duration_ms=6500)

    def toggle_chroma_green(self):
        self.is_chroma_green = not self.is_chroma_green
        self.cfg["chroma_green"] = self.is_chroma_green
        save_ui_config(self.cfg)
        if self.is_chroma_green:
            try:
                self.root.wm_attributes("-transparentcolor", "")
            except Exception:
                pass
            self.root.configure(bg="#00FF00")
            self.canvas.configure(bg="#00FF00")
            self._load_sprites()
            self.set_state(self.current_state)
            self.show_speech_bubbles([
                "已切换为窗口捕捉绿幕模式！",
                "在直播姬/OBS添加【窗口捕捉】选择 [哈基米桌宠]，然后添加【色度键】滤镜滤除绿色背景即可~"
            ], duration_ms=6000)
        else:
            self.root.configure(bg=TRANSPARENT_KEY)
            self.canvas.configure(bg=TRANSPARENT_KEY)
            try:
                self.root.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
            except Exception:
                pass
            self._load_sprites()
            self.set_state(self.current_state)
            self.show_speech_bubbles(["已恢复普通桌面透明背景~"], duration_ms=3500)

    def set_mic_hotkey(self, hotkey_name: str):
        self.cfg["mic_hotkey"] = hotkey_name
        save_ui_config(self.cfg)
        self._init_context_menu()
        self.show_speech_bubbles([f"麦克风快捷键已设置为：{hotkey_name}！随时在任何游戏或窗口下按该快捷键唤醒本喵~"], duration_ms=4500)

    def _poll_global_hotkey(self):
        if os.name == "nt":
            try:
                user32 = ctypes.windll.user32
                preset = self.cfg.get("mic_hotkey", "Alt+V")
                is_down = False
                if preset == "Alt+V":
                    is_down = bool((user32.GetAsyncKeyState(0x12) & 0x8000) and (user32.GetAsyncKeyState(0x56) & 0x8000))
                elif preset == "F8":
                    is_down = bool(user32.GetAsyncKeyState(0x77) & 0x8000)
                elif preset == "Ctrl+Shift+V":
                    is_down = bool(
                        (user32.GetAsyncKeyState(0x11) & 0x8000)
                        and (user32.GetAsyncKeyState(0x10) & 0x8000)
                        and (user32.GetAsyncKeyState(0x56) & 0x8000)
                    )

                if is_down and not self._hotkey_was_down:
                    self._hotkey_was_down = True
                    self.toggle_voice_record()
                elif not is_down and self._hotkey_was_down:
                    self._hotkey_was_down = False
            except Exception:
                pass
        self.root.after(60, self._poll_global_hotkey)

    def quit_app(self):
        if hasattr(self, "live_server"):
            self.live_server.stop()
        if hasattr(self, "recorder") and self.recorder.is_recording:
            try:
                self.recorder.stop()
            except Exception:
                pass
        self.cfg["x"] = self.root.winfo_x()
        self.cfg["y"] = self.root.winfo_y()
        save_ui_config(self.cfg)
        global _SINGLETON_MUTEX_HANDLE
        if _SINGLETON_MUTEX_HANDLE and os.name == "nt":
            try:
                ctypes.windll.kernel32.CloseHandle(_SINGLETON_MUTEX_HANDLE)
            except Exception:
                pass
            _SINGLETON_MUTEX_HANDLE = None
        self.bridge.close()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


_SINGLETON_MUTEX_HANDLE = None


def _append_boot_log(message: str) -> None:
    try:
        log_path = ROOT_DIR / "state" / "desktop_pet_boot.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def ensure_single_instance() -> bool:
    """Return True if this is the only instance, or False if another instance is running."""
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ERROR_ALREADY_EXISTS = 183
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, "Local\\HakimiDesktopPetSingleton")
        err = ctypes.get_last_error()
        if not handle:
            _append_boot_log(f"CreateMutexW returned NULL (last_error={err}); continuing.")
            return True
        if err == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        global _SINGLETON_MUTEX_HANDLE
        _SINGLETON_MUTEX_HANDLE = handle
        return True
    except Exception as exc:
        _append_boot_log(f"ensure_single_instance exception: {exc}")
        return True


if __name__ == "__main__":
    if not ensure_single_instance():
        _append_boot_log("Skipped startup: HakimiDesktopPetSingleton already exists.")
        sys.exit(0)
    _append_boot_log(f"Starting HakimiDesktopPet pid={os.getpid()}")
    try:
        HakimiDesktopPet().run()
        _append_boot_log(f"HakimiDesktopPet exited cleanly pid={os.getpid()}")
    except Exception:
        import traceback

        _append_boot_log(f"HakimiDesktopPet crashed pid={os.getpid()}:\n{traceback.format_exc()}")
        raise
