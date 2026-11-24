"""Local Windows PC action executor with deterministic owner-approval boundaries.

Nothing in this module runs automatically from model output. Every action is
invoked only after the user explicitly clicks the [允许执行] button in the
Desktop Pet UI.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import webbrowser

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

SAFE_APP_ALIASES = {
    "notepad": "notepad.exe",
    "calc": "calc.exe",
    "calculator": "calc.exe",
    "explorer": "explorer.exe",
    "mspaint": "mspaint.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "taskmgr": "taskmgr.exe",
    "code": "code",
}

VK_MAP = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "mediaplaypause": 0xB3,
    "medianext": 0xB0,
    "mediaprev": 0xB1,
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
}


def execute_approved_action(action: dict, tk_root=None) -> str:
    """Execute a single owner-approved pc_* action and return a human-readable result."""
    tool = str(action.get("tool", ""))
    args = dict(action.get("args") or {})
    try:
        if tool == "pc_open_target":
            target = str(args.get("target", "")).strip()
            if not target:
                return "未指定要打开的目标"
            if target.startswith(("http://", "https://")):
                webbrowser.open(target)
                return f"已在浏览器打开：{target}"
            alias = SAFE_APP_ALIASES.get(target.lower())
            if alias:
                subprocess.Popen([alias])
                return f"已启动应用：{alias}"
            p = Path(os.path.expandvars(target)).expanduser()
            if p.exists():
                os.startfile(str(p))  # type: ignore[attr-defined]
                return f"已打开路径：{p}"
            subprocess.Popen(target, shell=True)
            return f"已尝试打开：{target}"

        if tool == "pc_clipboard_set":
            text = str(args.get("text", ""))
            if tk_root is not None:
                tk_root.clipboard_clear()
                tk_root.clipboard_append(text)
                tk_root.update()
            else:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", "Set-Clipboard -Value $input"],
                    input=text,
                    text=True,
                    encoding="utf-8",
                    creationflags=CREATE_NO_WINDOW,
                    timeout=5,
                )
            return f"已写入剪贴板（共 {len(text)} 字符）"

        if tool == "pc_run_command":
            cmd = str(args.get("command", "")).strip()
            if not cmd:
                return "命令为空"
            res = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25,
                creationflags=CREATE_NO_WINDOW,
            )
            out = (res.stdout or "").strip()
            err = (res.stderr or "").strip()
            combined = out if out else err
            if not combined:
                combined = f"执行完成 (退出码 {res.returncode})"
            return f"命令结果 (code={res.returncode}):\n{combined[:500]}"

        if tool == "pc_input_action":
            act_type = str(args.get("action_type", "")).strip()
            detail = str(args.get("detail", "")).strip()
            if act_type == "press_key" and detail.lower() in VK_MAP and os.name == "nt":
                vk = VK_MAP[detail.lower()]
                ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
                ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
                return f"已触发按键：{detail}"
            # Use WScript.Shell SendKeys for hotkeys / typing
            send_str = detail
            if act_type == "hotkey":
                # Convert e.g. ctrl+s -> ^s, alt+f4 -> %{F4}
                parts = [p.strip().lower() for p in detail.split("+")]
                mods = ""
                key_part = ""
                for p in parts:
                    if p in ("ctrl", "control"):
                        mods += "^"
                    elif p == "alt":
                        mods += "%"
                    elif p == "shift":
                        mods += "+"
                    else:
                        key_part = p if len(p) == 1 else "{" + p.upper() + "}"
                send_str = mods + key_part
            ps_script = (
                "$ws = New-Object -ComObject WScript.Shell; "
                "Start-Sleep -Milliseconds 250; "
                f"$ws.SendKeys({json.dumps(send_str)})"
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps_script],
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
            return f"已发送键盘动作：{act_type} ({detail})"

        return f"未知的本地操作工具：{tool}"
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {str(exc)[:200]}"
