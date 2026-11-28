"""Bounded local, read-only tools. Chat text is data, never tool authority.

This is an accidental-misuse boundary, not an operating-system sandbox. The
trusted waiter creates active_context.json; QQ messages may not edit it.
"""
from hakimi import paths
from hakimi.chat.turn_lanes import active_context_path

import ast
import json
import math
import operator
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from hakimi.core.time_tool import clock_snapshot

BASE_DIR = paths.base()
SESSION_RE = re.compile(r"(?:group|private):[1-9][0-9]{0,19}\Z")
MAX_TAIL_BYTES = 512 * 1024
MAX_LINE_BYTES = 32 * 1024
MAX_LEASE_SECONDS = 600
MAX_VALUE = 10 ** 100


class ToolError(ValueError):
    """A safe, user-facing validation error."""


def _fixed_state_file(base_dir, name):
    base = Path(base_dir).resolve()
    state = base / "state"
    path = state / name
    # Reject redirected state folders and files, even if still under base.
    if state.is_symlink() or state.resolve() != state or path.is_symlink() or path.resolve() != path:
        raise ToolError("状态路径被重定向，拒绝读取")
    return path


def require_context(session_key, base_dir=BASE_DIR, now=None):
    if not isinstance(session_key, str) or not SESSION_RE.fullmatch(session_key):
        raise ToolError("session 必须是当前 group:<数字> 或 private:<数字>")
    now = time.time() if now is None else now
    path = _fixed_state_file(base_dir, active_context_path(base_dir).name)
    try:
        with path.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ToolError("当前会话凭据过大")
        context = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ToolError("没有有效的当前会话凭据，请等待下一次唤醒") from exc
    if not isinstance(context, dict) or context.get("session_key") != session_key:
        raise ToolError("拒绝跨会话操作：session 与当前唤醒不一致")
    issued, expires = context.get("issued_at"), context.get("expires_at")
    if (type(issued) not in (int, float) or type(expires) not in (int, float)
            or not math.isfinite(issued) or not math.isfinite(expires)
            or not issued <= now < expires or not 0 < expires - issued <= MAX_LEASE_SECONDS):
        raise ToolError("当前会话凭据已过期或时间无效，请等待下一次唤醒")
    trigger = context.get("trigger_id")
    if not isinstance(trigger, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", trigger):
        raise ToolError("当前会话凭据缺少有效 trigger_id")
    if context.get("owner_token"):
        from hakimi.bridge.turn_coordinator import TurnCoordinator
        if context.get("provider") not in {"gemini","dsh"} or not TurnCoordinator(base_dir).check_token(context["owner_token"],context["provider"]):
            raise ToolError("本轮已完成或被接管，旧凭据失效")
    return context


def parse_input(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096:
        raise ToolError("input 必须是最多 4096 字节的 JSON 对象")

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ToolError("JSON 不允许重复字段")
            result[key] = value
        return result

    try:
        data = json.loads(raw, object_pairs_hook=object_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ToolError("JSON 不允许非有限数字")))
    except (ValueError, RecursionError) as exc:
        raise ToolError("input 必须是合法 JSON 对象（不得包含重复字段或非有限数字）") from exc
    if not isinstance(data, dict):
        raise ToolError("input 必须是 JSON 对象")
    return data


def _schema(data, allowed, required=()):
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        raise ToolError("工具输入字段不符合 schema")


def calculate(expression):
    if not isinstance(expression, str) or not 1 <= len(expression.strip()) <= 256:
        raise ToolError("expression 长度须为 1 到 256")
    expression = expression.strip()
    try:
        root = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ToolError("算式语法无效") from exc
    if sum(1 for _ in ast.walk(root)) > 64:
        raise ToolError("算式过于复杂（最多 64 个语法节点）")
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
              ast.Mod: operator.mod, ast.Pow: operator.pow}

    def bounded(value):
        if type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)) or abs(value) > MAX_VALUE:
            raise ToolError("计算结果超限或不是有限实数")
        return value

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return bounded(node.value)
        if isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub):
            value = visit(node.operand)
            return bounded(value if isinstance(node.op, ast.UAdd) else -value)
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ToolError("次幂指数绝对值最多 100")
            try:
                return bounded(binary[type(node.op)](left, right))
            except (ArithmeticError, ValueError) as exc:
                raise ToolError("算式无法计算、除数为零或结果超限") from exc
        raise ToolError("只允许数字、括号和 + - * / // % **；不允许名称或函数")

    return visit(root)


def _tail_records(path):
    """Read a fixed-size tail; tolerate partial appends and damaged JSON lines."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - MAX_TAIL_BYTES)
            stream.seek(start)
            raw = stream.read(MAX_TAIL_BYTES)
        if start:
            raw = raw.partition(b"\n")[2]
        # Ignore an incomplete trailing line, which the daemon may be writing.
        complete = raw.rsplit(b"\n", 1)[0] if not raw.endswith(b"\n") else raw
        records = []
        for line in complete.splitlines()[-2000:]:
            if len(line) > MAX_LINE_BYTES:
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if isinstance(record, dict):
                records.append(record)
        return records, size > MAX_TAIL_BYTES
    except FileNotFoundError:
        return [], False
    except OSError as exc:
        raise ToolError("本地记录暂时无法读取") from exc


def _text(value, length):
    return value[:length] if isinstance(value, str) else ""


def context_evidence(session_key, limit, base_dir):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ToolError("limit 必须是 1 到 50 的整数")
    records, tail_limited = _tail_records(_fixed_state_file(base_dir, "chat_history.jsonl"))
    matched = [r for r in records if r.get("session_key") == session_key]
    selected = matched[-limit:]
    speakers = {}
    messages = []
    from hakimi.chat.image_permissions import allowed_images_for_session
    from hakimi.core.media_codes import compact_media
    try:
        images = allowed_images_for_session(session_key, Path(base_dir))
    except (OSError, ValueError):
        images = []
    for record in selected:
        # Pseudonymous speaker references avoid copying QQ numbers into summaries.
        uid = str(record.get("user_id", ""))[:30]
        speaker = speakers.setdefault(uid, f"speaker_{len(speakers) + 1}")
        original = record.get("text", "")
        # Same stable image codes as the prompt; no cache paths or CDN links.
        if isinstance(original, str):
            original = compact_media(original, images)
        text = _text(original, 800)
        message_id = record.get("message_id")
        if type(message_id) not in (int, str):
            message_id = None
        elif isinstance(message_id, str):
            message_id = message_id[:80]
        messages.append({
            "reference": f"M{len(messages) + 1}", "message_id": message_id,
            "recorded_time": _text(record.get("timestamp") or record.get("time"), 40),
            "speaker": speaker, "nickname": _text(record.get("nickname"), 64),
            "text_untrusted": text,
            "text_truncated": isinstance(original, str) and len(original) > 800,
        })
    return {"kind": "extractive_evidence_only", "messages": messages,
            "counts": {"messages_returned": len(messages), "distinct_speakers": len(speakers)},
            "scan_tail_limited": tail_limited,
            "more_session_messages_in_scanned_tail": len(matched) > limit}


def status_snapshot(session_key, context, base_dir, now):
    def marker(name):
        path = _fixed_state_file(base_dir, name)
        try:
            stat = path.stat()
            return {"present": True, "age_seconds": round(max(0, now - stat.st_mtime), 1)}
        except FileNotFoundError:
            return {"present": False}
        except OSError as exc:
            raise ToolError("状态文件暂时无法读取") from exc

    rows, _ = _tail_records(_fixed_state_file(base_dir, "chat_history.jsonl"))
    return {"current_context_seconds_remaining": max(0, int(context["expires_at"] - now)),
            "online_marker": marker("qq_online.json"), "waiting_marker": marker("waiting.flag"),
            "session_records_in_recent_local_tail": sum(r.get("session_key") == session_key for r in rows)}


def run_tool(name, session_key, data, base_dir=BASE_DIR, now=None):
    now = time.time() if now is None else now
    context = require_context(session_key, base_dir, now)
    common_limits = ["返回结果对应本轮会话和查询时刻。"]
    if name == "calculator":
        _schema(data, ("expression",), ("expression",))
        output = {"expression": data["expression"], "value": calculate(data["expression"])}
        source = "bounded_python_ast_arithmetic"
        limits = ["数值算式采用Python实数与浮点运算，保留浮点舍入精度。"]
    elif name == "context":
        _schema(data, ("limit",))
        output = context_evidence(session_key, data.get("limit", 20), base_dir)
        source = "current_session_local_history_tail"
        limits = ["本地缓存的消息摘录与计数，按M编号定位原文，汇总时区分事实、观点和待核对项。",
                  "时间字段表示原记录时间；每条正文800字以内，窗口读取最后512KiB/2000行的完整记录。"]
    elif name == "status":
        _schema(data, ())
        output = status_snapshot(session_key, context, base_dir, now)
        source = "local_runtime_marker_snapshot"
        limits = ["状态标记的生成时间和字段共同描述这次本机快照。"]
    elif name == "time":
        _schema(data, ("timezone",))
        try:
            output = clock_snapshot(data.get("timezone", "local"), now)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        source = "system_clock"
        limits = ["系统时钟查询时刻，时区和UTC偏移随结果给出。",
                  "local表示运行电脑时区，Asia/Shanghai表示北京时间UTC+08:00。"]
    elif name in {"bilibili_video", "qq_miniapp_video"}:
        _schema(data, ("url",), ("url",)) if name == "bilibili_video" else _schema(data, ("message_id",))
        from hakimi.tools.bilibili_video import find_url, inspect_for_primary
        from hakimi.core.context_window import select_context
        from hakimi.bridge.turn_coordinator import TurnCoordinator
        payload = TurnCoordinator(base_dir).snapshot().get("payload", {})
        rows, _ = select_context(payload)
        if name == "qq_miniapp_video":
            selected = data.get("message_id")
            row = next((row for row in reversed(rows) if row.get("user_id") != "SELF"
                        and (not selected or str(row.get("message_id")) == str(selected))
                        and find_url(row.get("text", ""))), None)
        else:
            url = find_url(data["url"])
            row = next((row for row in rows if url and find_url(row.get("text", "")) == url), None)
        if not row:
            raise ToolError("本轮当前群未提供可观看的B站地址；若QQ小程序仅有标题或封面，请分享者复制原视频链接")
        output = inspect_for_primary(payload, Path(base_dir), source=row, owner=context.get("owner_token"),
             is_active=lambda: TurnCoordinator(base_dir).check_token(context.get("owner_token"), "gemini"),
            timeout_seconds=max(1,min(180,context.get("expires_at",time.time()+182)-time.time()-2)))
        current = require_context(session_key, base_dir)
        paths = [f["path"] for f in (output or {}).get("key_frames", [])]
        if paths:
            current["allowed_image_paths"] = list(dict.fromkeys(current.get("allowed_image_paths", [])+paths))
            target = _fixed_state_file(base_dir, active_context_path(base_dir).name)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
        source = "bilibili_companion_qq_on_demand"
        limits = ["内容来自视频机器人实际音视频理解；coverage为有证据的时段，sampled_intervals为采样范围。"]
    else:
        raise ToolError("未知工具；仅允许 calculator、context、status、time、bilibili_video、qq_miniapp_video")
    return {"version": 1, "tool": name, "session_key": session_key,
            "trigger_id": context["trigger_id"],
            "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "source": source, "data": output, "limits": common_limits + limits}
