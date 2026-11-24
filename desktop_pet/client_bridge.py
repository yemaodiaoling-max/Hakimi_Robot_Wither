"""Persistent SSH bridge & screen capture module for the Hakimi Desktop Pet.

Reads desktop_pet/pet_connection.json (see connection.py): the server side runs as a local
child process, or over one windowless, persistent SSH stream to desktop_pet_agent.py --serve.
Automatically reconnects after sleep/wake or network interruptions.
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from PIL import Image, ImageGrab

ROOT_DIR = Path(__file__).resolve().parent.parent
from desktop_pet import connection as _connection
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def get_active_window_title() -> str:
    """Return the title of the currently focused Windows application."""
    if os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value.strip()[:160]
    except Exception:
        return ""


def capture_screen_b64(max_width: int = 1366, quality: int = 80) -> tuple[str, str]:
    """Capture the primary screen, resize to bounded JPEG, and return (base64_str, mime)."""
    img = ImageGrab.grab(all_screens=False)
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    if w > max_width:
        new_h = max(1, int(h * (max_width / w)))
        img = img.resize((max_width, new_h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


class VoiceRecorder:
    """Zero-dependency Windows microphone recorder using MCI waveaudio."""

    def __init__(self, alias: str = "hakimi_mic"):
        self.alias = alias
        self._is_recording = False
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    def _send(self, cmd: str) -> int:
        if os.name != "nt":
            return -1
        try:
            return ctypes.windll.winmm.mciSendStringW(cmd, None, 0, None)
        except Exception:
            return -1

    def start(self) -> bool:
        with self._lock:
            if os.name != "nt":
                return False
            self._send(f"close {self.alias}")
            ret = self._send(f"open new type waveaudio alias {self.alias}")
            if ret != 0:
                return False
            self._send(f"set {self.alias} time format ms bitspersample 16 channels 1 samplespersec 16000 bytespersec 32000 alignment 2")
            ret = self._send(f"record {self.alias}")
            if ret == 0:
                self._is_recording = True
                return True
            self._send(f"close {self.alias}")
            return False

    def stop(self) -> tuple[str | None, str]:
        with self._lock:
            if not self._is_recording:
                return None, "audio/wav"
            self._is_recording = False
            temp_path = None
            try:
                fd, temp_path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                self._send(f"stop {self.alias}")
                save_ret = self._send(f'save {self.alias} "{temp_path}"')
                self._send(f"close {self.alias}")
                if save_ret != 0 or not os.path.exists(temp_path):
                    return None, "audio/wav"
                data = Path(temp_path).read_bytes()
                if len(data) < 1000:
                    return None, "audio/wav"
                return base64.b64encode(data).decode("ascii"), "audio/wav"
            except Exception:
                return None, "audio/wav"
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except Exception:
                        pass
                self._send(f"close {self.alias}")


class ServerBridge:
    """Thread-safe persistent JSONL client over OpenSSH to the VPS desktop pet agent."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self.last_status: dict = {}

    def _ssh_command(self, serve: bool = True) -> list[str]:
        return _connection.command(serve=serve)

    def _ensure_connected(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self.close()
        proc = subprocess.Popen(
            self._ssh_command(serve=True),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        # Read initial ready handshake line
        first_line = proc.stdout.readline().strip() if proc.stdout else ""
        if not first_line:
            err = proc.stderr.read(400) if proc.stderr else ""
            proc.kill()
            raise RuntimeError(f"连接服务器桌宠后端失败: {err.strip() or '无响应'}")
        self._proc = proc
        return proc

    def request(self, payload: dict) -> dict:
        with self._lock:
            for attempt in range(2):
                try:
                    proc = self._ensure_connected()
                    assert proc.stdin is not None and proc.stdout is not None
                    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                    line = proc.stdout.readline().strip()
                    if not line:
                        raise RuntimeError("SSH 通道已断开")
                    data = json.loads(line)
                    if isinstance(data, dict) and data.get("context"):
                        self.last_status = data["context"]
                    return data
                except Exception as exc:
                    self.close()
                    if attempt == 1:
                        return {"ok": False, "error": str(exc)}
                    time.sleep(0.4)
            return {"ok": False, "error": "未知连接错误"}

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None
