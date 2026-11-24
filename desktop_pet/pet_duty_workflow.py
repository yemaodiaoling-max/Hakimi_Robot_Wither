"""Zero-coordinator run_workflow loop for Hakimi Desktop Pet duty shifts.

Runs pet_waiter.py --json in Python without waking a parent LLM coordinator,
rotating to a fresh low-cost agent context every max_turns and overwriting the
rolling summary block in state/pet_duty_handoff.md.
"""
import asyncio
import json
import os
from pathlib import Path

ROOT_DIR = Path(WORKSPACES[0]) if WORKSPACES else Path.cwd()
import sys as _sys
PYTHON_EXE = _sys.executable
HANDOFF_FILE = ROOT_DIR / "state" / "pet_duty_handoff.md"
VALID_MODELS = {"inherit", "flash_lite", "flash", "pro"}
VALID_SPRITES = {"happy", "peek", "thinking", "idle"}

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "bubbles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "1~3条桌宠短气泡内容",
        },
        "sprite": {
            "type": "string",
            "enum": ["happy", "peek", "thinking", "idle"],
        },
        "private": {
            "type": "boolean",
            "description": "主人是否正在与他人私聊/单聊（是则为 true）",
        },
        "rotate_summary": {
            "type": "string",
            "description": "仅满额交接轮次填写 1~2 句中文接力脉络（不含私聊），其余轮次留空字符串",
        },
    },
    "required": ["bubbles", "sprite", "private", "rotate_summary"],
}

raw_model = str(args.get("model", "inherit"))
MODEL = raw_model if raw_model in VALID_MODELS else "inherit"
MAX_SHIFTS = max(1, int(args.get("max_shifts", 30)))
MAX_CONSECUTIVE_ERRORS = max(1, int(args.get("max_consecutive_errors", 5)))
DRY_RUN = bool(args.get("dry_run", False))


async def _run_py(*cmd_args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        PYTHON_EXE,
        *cmd_args,
        cwd=str(ROOT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    return (
        int(proc.returncode or 0),
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


def _parse_json(text: str) -> dict:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    continue
        return {}


async def main() -> None:
    rc, sync_out, sync_err = await _run_py("desktop_pet/pet_cli.py", "handoff-sync")
    if rc != 0:
        log(f"handoff-sync failed rc={rc}: {sync_err.strip() or sync_out.strip()}")
        if DRY_RUN:
            print(json.dumps({"ok": False, "error": "handoff_sync_failed", "rc": rc}, ensure_ascii=False))
        return

    if DRY_RUN:
        handoff_ok = False
        if HANDOFF_FILE.exists():
            text = HANDOFF_FILE.read_text(encoding="utf-8")
            start_idx = text.find("<!-- SUMMARY_START -->")
            end_idx = text.find("<!-- SUMMARY_END -->")
            handoff_ok = start_idx != -1 and end_idx != -1 and start_idx < end_idx
        print(json.dumps({
            "ok": Path(PYTHON_EXE).exists() and handoff_ok,
            "dry_run": True,
            "python_exists": Path(PYTHON_EXE).exists(),
            "handoff_file": str(HANDOFF_FILE),
            "handoff_markers_ok": handoff_ok,
            "model": MODEL,
            "max_shifts": MAX_SHIFTS,
        }, ensure_ascii=False))
        return

    shift = 1
    duty = None
    consecutive_errors = 0
    active_waiter = None

    try:
        while shift <= MAX_SHIFTS:
            active_waiter = await asyncio.create_subprocess_exec(
                PYTHON_EXE,
                "desktop_pet/pet_waiter.py",
                "--json",
                "--parent-pid",
                str(os.getpid()),
                cwd=str(ROOT_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await active_waiter.communicate()
            waiter_rc = int(active_waiter.returncode or 0)
            active_waiter = None

            stdout_str = stdout_b.decode("utf-8", errors="replace")
            stderr_str = stderr_b.decode("utf-8", errors="replace")
            turn_info = _parse_json(stdout_str)

            if waiter_rc != 0 or not turn_info.get("ok") or not turn_info.get("turn_id"):
                consecutive_errors += 1
                log(
                    f"waiter 退出异常 rc={waiter_rc} err={stderr_str.strip()[:200]} "
                    f"({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS})"
                )
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    log("连续错误达到上限，停止桌宠工作流。")
                    break
                await asyncio.sleep(2)
                continue

            if duty is None:
                handoff_text = HANDOFF_FILE.read_text(encoding="utf-8")
                standing_prompt = (
                    f"{handoff_text}\n\n"
                    "【当前模式：Workflow 结构化输出】\n"
                    "1. 如有 screen_path，先用 read_file 查看桌面截图。\n"
                    "2. 不要自行运行 pet_waiter.py、pet_cli.py reply 或 pet_cli.py rotate。\n"
                    "3. 直接调用 finish 返回 JSON（bubbles, sprite, private, rotate_summary）。"
                )
                phase(f"第 {shift} 班")
                duty = agent(
                    standing_prompt,
                    schema=TURN_SCHEMA,
                    role=f"哈基米值守 第{shift}班",
                    type_name="self",
                    workspace="inherit",
                    model=MODEL,
                )

            turn_id = str(turn_info["turn_id"])
            try:
                res = await duty(str(turn_info.get("prompt", "")), TURN_SCHEMA)
                raw_bubbles = res.get("bubbles") if isinstance(res, dict) else None
                bubbles = [str(b).strip() for b in (raw_bubbles or []) if str(b).strip()]
                if not bubbles:
                    bubbles = ["喵？"]
                sprite_val = str(res.get("sprite", "happy") if isinstance(res, dict) else "happy")
                sprite = sprite_val if sprite_val in VALID_SPRITES else "happy"
                is_private = bool(res.get("private", False)) if isinstance(res, dict) else False
                rotate_summary = str(res.get("rotate_summary", "") or "").strip() if isinstance(res, dict) else ""

                reply_cmd = [
                    "desktop_pet/pet_cli.py",
                    "reply",
                    "--turn-id",
                    turn_id,
                    "--text",
                    "|".join(bubbles),
                    "--sprite",
                    sprite,
                ]
                if is_private:
                    reply_cmd.append("--private")

                rep_rc, rep_out, rep_err = await _run_py(*reply_cmd)
                reply_json = _parse_json(rep_out)
                if rep_rc != 0 or not reply_json.get("ok"):
                    log(f"第 {shift} 班 reply 异常 rc={rep_rc}: {rep_err.strip() or rep_out.strip()}")

                should_rotate = bool(reply_json.get("should_rotate") or turn_info.get("should_rotate_after"))
                if should_rotate:
                    rot_rc, rot_out, rot_err = await _run_py(
                        "desktop_pet/pet_cli.py",
                        "rotate",
                        "--summary",
                        rotate_summary,
                    )
                    if rot_rc != 0:
                        log(f"第 {shift} 班 rotate 异常 rc={rot_rc}: {rot_err.strip() or rot_out.strip()}")
                    else:
                        log(f"第 {shift} 班满额轮转完成")
                    del duty
                    duty = None
                    shift += 1

                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                log(f"第 {shift} 班处理 turn={turn_id} 异常 ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {exc}")
                await _run_py(
                    "desktop_pet/pet_cli.py",
                    "reply",
                    "--turn-id",
                    turn_id,
                    "--text",
                    "喵？刚才走神卡了一下，再叫我一次嘛",
                    "--sprite",
                    "thinking",
                )
                if duty is not None:
                    del duty
                    duty = None
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    log("连续错误达到上限，停止桌宠工作流。")
                    break
    finally:
        if active_waiter is not None and active_waiter.returncode is None:
            active_waiter.terminate()
            try:
                await asyncio.wait_for(active_waiter.wait(), timeout=3)
            except Exception:
                active_waiter.kill()
        if duty is not None:
            del duty


await main()
