"""Idempotent launcher and Antigravity hook handler for the Hakimi Desktop Pet."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

PET_DIR = Path(__file__).resolve().parent
ROOT_DIR = PET_DIR.parent
UI_STATE_PATH = ROOT_DIR / "state" / "desktop_pet_ui.json"
BOOT_LOG_PATH = ROOT_DIR / "state" / "desktop_pet_boot.log"
WAITING_FLAG_PATH = ROOT_DIR / "state" / "pet_agy_waiting.flag"
HOOK_COOLDOWN_PATH = ROOT_DIR / "state" / "pet_agy_hook_cooldown.json"
MUTEX_NAME = "Local\\HakimiDesktopPetSingleton"
SYNCHRONIZE = 0x00100000
WAITER_FRESH_SEC = 15.0
HOOK_COOLDOWN_SEC = 60.0


def _log(msg: str) -> None:
    try:
        BOOT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(BOOT_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] [ensure_pet_running] {msg}\n")
    except Exception:
        pass


def _is_autostart_disabled() -> bool:
    try:
        if UI_STATE_PATH.exists():
            data = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("autostart_enabled") is False:
                return True
    except Exception:
        pass
    return False


def _is_pet_running() -> bool:
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def _launch_via_schtasks() -> bool:
    if os.name != "nt":
        return False
    try:
        res = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", "HakimiDesktopPet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=0x08000000,
            timeout=4,
        )
        if res.returncode == 0:
            _log("Triggered Scheduled Task HakimiDesktopPet via schtasks.exe")
            return True
    except Exception as exc:
        _log(f"schtasks.exe trigger failed: {exc}")
    return False


def _launch_pet(direct: bool = False) -> bool:
    if not direct and _launch_via_schtasks():
        return True

    pythonw = ROOT_DIR.parent / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    pet_script = PET_DIR / "pet_app.py"

    creationflags = 0
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB

    try:
        proc = subprocess.Popen(
            [str(pythonw), str(pet_script)],
            cwd=str(ROOT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError:
        fallback_flags = (0x00000008 | 0x00000200 | 0x08000000) if os.name == "nt" else 0
        proc = subprocess.Popen(
            [str(pythonw), str(pet_script)],
            cwd=str(ROOT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=fallback_flags,
        )
    _log(f"Spawned pet_app.py detached pid={proc.pid}")
    return True


def _is_waiter_active() -> bool:
    try:
        if not WAITING_FLAG_PATH.exists():
            return False
        raw = WAITING_FLAG_PATH.read_text(encoding="utf-8").strip()
        ts = float(raw) if raw else WAITING_FLAG_PATH.stat().st_mtime
        return (time.time() - ts) < WAITER_FRESH_SEC
    except Exception:
        return False


def _should_inject_attach_notice(payload: dict) -> bool:
    if _is_autostart_disabled() or _is_waiter_active():
        return False

    inv_num = payload.get("invocationNum")
    if isinstance(inv_num, int) and inv_num > 1:
        return False

    last_input = str(payload.get("lastUserInput") or "")
    if any(k in last_input for k in (
        "pet_waiter.py",
        "pet_duty_workflow.py",
        "pet_duty_handoff.md",
        "Hakimi Pet Hydrogen Duty Agent",
        "pet-duty-",
    )):
        return False

    now = time.time()
    try:
        if HOOK_COOLDOWN_PATH.exists():
            cd = json.loads(HOOK_COOLDOWN_PATH.read_text(encoding="utf-8"))
            if isinstance(cd, dict) and (now - float(cd.get("last_injected_at", 0.0))) < HOOK_COOLDOWN_SEC:
                return False
    except Exception:
        pass

    try:
        HOOK_COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        HOOK_COOLDOWN_PATH.write_text(
            json.dumps({"last_injected_at": now, "conversationId": payload.get("conversationId", "")}),
            encoding="utf-8",
        )
    except Exception:
        pass
    return True


def _build_attach_hook_response() -> dict:
    workflow_script = PET_DIR / "pet_duty_workflow.py"
    handoff_file = ROOT_DIR / "state" / "pet_duty_handoff.md"
    msg = (
        "【哈基米桌宠自动挂接提醒】检测到本地哈基米桌宠正在运行，但当前没有活跃的 Antigravity 值守代理"
        "（state/pet_agy_waiting.flag 未激活）。"
        f"首选方案（零协调者 Token）：在 `Gemini 4 Hydrogen (Low)` 专用会话使用 `run_workflow` 运行 `{workflow_script}`"
        "（`Args: {\"model\": \"inherit\"}`，高档主会话可传 `\"flash\"`），自动完成 10 轮轮转并覆写交接文件。"
        f"备选方案（高档主会话需指定 `gemini-4-hydrogen-low` 时）：使用极简 `swarm` 创建单人值守 worker"
        "（`model: \"gemini-4-hydrogen-low\"`，班次名 `pet-duty-1`，严禁添加 monitor/narrator），"
        f"brief 仅需一句让其先 `read_file` 读取 `{handoff_file}` 并按其中规则启动值守；"
        "满 10 轮执行 `pet_cli.py rotate` 覆写摘要后严禁重启 `pet_waiter.py`，仅发一句极简交棒消息给 `@coordinator` 换班。"
        "（注：如果你自身就是值守代理，请直接忽略本提示。）"
    )
    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": msg,
                }
            }
        ]
    }


def main() -> int:
    is_hook = "--hook" in sys.argv
    force = "--force" in sys.argv
    direct = "--direct" in sys.argv
    hook_payload: dict = {}

    if is_hook:
        try:
            if sys.stdin is not None and not sys.stdin.isatty():
                raw_in = sys.stdin.read().strip()
                if raw_in:
                    parsed = json.loads(raw_in)
                    if isinstance(parsed, dict):
                        hook_payload = parsed
        except Exception:
            pass

    try:
        if not force and _is_autostart_disabled():
            pass
        elif not _is_pet_running():
            _launch_pet(direct=direct)
    except Exception as exc:
        _log(f"Error ensuring pet running: {exc}")

    if is_hook:
        out_obj: dict = {}
        try:
            if _should_inject_attach_notice(hook_payload):
                out_obj = _build_attach_hook_response()
                _log("Injected auto-attach reminder for Hydrogen duty agent.")
        except Exception as exc:
            _log(f"Hook inject check error: {exc}")
        sys.stdout.write(json.dumps(out_obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
