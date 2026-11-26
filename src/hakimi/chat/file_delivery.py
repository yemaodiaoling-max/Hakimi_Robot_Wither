"""Shared file selection and broker-created text attachments; no background jobs."""
import re
import os
import stat
from pathlib import Path
from urllib.parse import quote, unquote

from hakimi.core.runtime_io import read_json_cached

DEFAULTS = {
    "extensions": {"image": [".png", ".jpg", ".jpeg", ".gif", ".webp"], "video": [".mp4"],
        "voice": [".silk", ".wav", ".mp3", ".flac"],
        "file": [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
                 ".txt", ".md", ".csv", ".json", ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"]},
    "max_mb": {"image": 30, "video": 100, "voice": 20, "file": 100},
    "upload_timeout_seconds": 180,
}
KINDS = {"image": 1, "video": 2, "voice": 3, "file": 4}
SEGMENT = re.compile(r"\[CQ:file,name=([^,\]]+),stamp=([0-9a-f]+-[0-9a-f]+),mode=(auto|file)\]\Z")


def settings(base):
    base = Path(base).resolve()
    root = base.parent if base.name == "official_test_runtime" else base
    try:cfg = read_json_cached(root/"file_send_config.json", {})
    except (OSError, ValueError):cfg = {}
    return {**DEFAULTS, **cfg,
            "extensions": {**DEFAULTS["extensions"], **cfg.get("extensions", {})},
            "max_mb": {**DEFAULTS["max_mb"], **cfg.get("max_mb", {})}}


def directory(session, base):
    if not re.fullmatch(r"(?:group|private):[1-9][0-9]{0,19}", str(session)):
        raise ValueError("无效会话")
    root = Path(base).resolve()/"state/outgoing_files"/session.replace(":", "_")
    if root.resolve() != root.absolute():raise ValueError("发送目录被重定向")
    return root


def validate_specs(specs):
    if not isinstance(specs, list) or not 1 <= len(specs) <= 6:raise ValueError("每轮文件数量超过消息余量")
    result = []
    for spec in specs:
        item = {"name": spec} if isinstance(spec, str) else dict(spec) if isinstance(spec, dict) else spec
        if not isinstance(item, dict) or set(item)-{"name", "content", "export", "message_id", "mode"}:
            raise ValueError("文件参数无效")
        name = item.get("name")
        if (not isinstance(name, str) or not 1 <= len(name) <= 180 or name != name.strip()
                or name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                or name.endswith((".", " ")) or Path(name).stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}):
            raise ValueError("文件名须为单个正常文件名，不能传任意电脑路径")
        if item.get("mode", "auto") not in {"auto", "file"}:raise ValueError("文件模式仅支持auto或file")
        if "export" in item and item["export"] is not True:raise ValueError("export须为true")
        if "message_id" in item:
            if (not item.get("export") or type(item["message_id"]) not in (str, int)
                    or not 1 <= len(str(item["message_id"])) <= 80):raise ValueError("聊天摘录消息编号无效")
            item["message_id"] = str(item["message_id"])
        if "content" in item and (item.get("export") or not isinstance(item["content"], str)
                or not 1 <= len(item["content"]) <= 24000 or Path(name).suffix.lower() not in {".txt", ".md", ".csv"}):
            raise ValueError("仅txt、md、csv可直接生成，正文最多24000字")
        if item.get("export") and Path(name).suffix.lower() not in {".txt", ".md"}:
            raise ValueError("聊天摘录使用txt或md")
        result.append(dict(item))
    if len({row["name"] for row in result}) != len(result):raise ValueError("同轮不要重复同名文件")
    return result


def resolve(name, session, base, mode="auto"):
    validate_specs([{"name": name, "mode": mode}])
    path = directory(session, base)/name
    if not path.is_file() or path.resolve() != path.absolute():raise ValueError("文件不在本群待发送目录中")
    cfg = settings(base)
    kind, stamp = _details(path, path.stat(), cfg, mode)
    return path, kind, stamp


def _details(path, info, cfg, mode="auto"):
    kind = next((kind for kind, suffixes in cfg["extensions"].items() if path.suffix.lower() in suffixes), "file")
    if kind not in KINDS:raise ValueError("不支持这个文件类型")
    if mode == "file":kind = "file"
    if not 0 < info.st_size <= float(cfg["max_mb"][kind])*1024*1024:raise ValueError("文件为空或超过该类型上传上限")
    return KINDS[kind], f"{info.st_mtime_ns:x}-{info.st_size:x}"


def menu(session, base, limit=16):
    root = directory(session, base)
    if not root.exists():return []
    cfg = settings(base)
    entries = []
    with os.scandir(root) as scan:
        for entry in scan:
            if entry.name.startswith(('_math_render_', '_drawing_')):continue
            try:entries.append((entry, entry.stat()))
            except OSError:continue
    rows = []
    for entry, info in sorted(entries, key=lambda pair:pair[1].st_mtime, reverse=True)[:64]:
        path = root/entry.name
        if path.suffix == ".tmp":continue
        try:
            validate_specs([path.name])
            if entry.is_symlink() or not stat.S_ISREG(info.st_mode):continue
            kind, _ = _details(path, info, cfg)
            rows.append({"name": path.name, "type": next(key for key,value in KINDS.items() if value == kind),
                         "bytes": info.st_size})
        except (ValueError, OSError):continue
        if len(rows) >= limit:break
    return rows


def transcript(payload, message_id=None):
    from hakimi.core.context_window import select_context
    rows, _ = select_context(payload)
    if message_id:
        rows = [row for row in rows if str(row.get("message_id")) == message_id]
        if not rows:raise ValueError("只能摘录本轮当前群可见消息")
    if not rows:raise ValueError("本轮没有可摘录的聊天记录")
    lines = ["聊天摘录（仅本轮本群可见内容，保留接收顺序；可能是节选）", ""]
    for row in rows:
        text = str(row.get("text", ""))
        text = re.sub(r"\[(群友发图|表情包图片)\(可用view_file看图\):[^\]]*\]", r"[\1，附件未嵌入]", text)
        text = re.sub(r"\[CQ:image,[^\]]*\]", "[图片附件未嵌入]", text)
        text = re.sub(r"\[CQ:file,name=([^,\]]+),[^\]]*\]", lambda m:"[附件："+unquote(m[1])+"]", text)
        lines.append(f"{row.get('time', '')} {row.get('nickname') or ('哈基米' if row.get('user_id') == 'SELF' else '群友')}: {text}")
    return "\n".join(lines)


def stage(specs, payload, base):
    """Called under the coordinator's winning-turn lock, before the outbox commit."""
    checked = validate_specs(specs)
    session = payload["session_key"]
    root = directory(session, base)
    generated = []
    for item in checked:
        if item['name'].startswith('_drawing_'):
            raise ValueError('绘图只随原请求发送')
        content = transcript(payload, item.get("message_id")) if item.get("export") else item.get("content")
        if content is not None:
            generated.append((root/item["name"], content.encode("utf-8")))
        else:resolve(item["name"], session, base, item.get("mode", "auto"))
    if generated:root.mkdir(parents=True, exist_ok=True)
    for path, data in generated:
        if path.exists() and path.stat().st_size == len(data) and path.read_bytes() == data:continue
        temporary = path.with_suffix(path.suffix+".tmp")
        if path.resolve() != path.absolute() or temporary.resolve() != temporary.absolute():raise ValueError("文件路径被重定向")
        temporary.write_bytes(data)
        temporary.replace(path)
    segments = []
    for item in checked:
        mode = item.get("mode", "auto")
        _, kind, stamp = resolve(item["name"], session, base, mode)
        if kind==3:
            from hakimi.voice.tts import explicit,hum_requested,prepared_for_turn
            hum=hum_requested(payload,base)
            if item['name'].startswith('_voice_hum_') and not hum:raise ValueError('原创短哼唱不能冒充指定歌曲或歌词朗读')
            if not (prepared_for_turn(base,payload,item['name']) or explicit(payload,base) or hum):
                raise ValueError('本轮未授权此语音')
        segments.append(f"[CQ:file,name={quote(item['name'], safe='')},stamp={stamp},mode={mode}]")
    return segments


def from_segment(text, session, base):
    found = SEGMENT.fullmatch(text)
    if not found:raise ValueError("无效文件消息")
    path, kind, stamp = resolve(unquote(found[1]), session, base, found[3])
    if stamp != found[2]:raise ValueError("排队后文件发生变化，请重新选择")
    return path, kind
