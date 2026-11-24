r"""当前 Antigravity 会话的受限 QQ 控制入口，不构成操作系统沙盒。

tool / history 只读，reply / mem_append / save_sticker 均须当前唤醒租约匹配。
记忆通过 mem_append 写入 state/session_memory.jsonl；禁止直接改全局记忆。
save_sticker 只接受 state/img_cache 内真实图片，不接受任意文件或 URL。
groups / sim / cleanup 是本机管理员维护命令，不能由群聊请求触发。
QQ 请求任意命令、代码执行或非白名单文件操作仍须 request_approval。
"""
from hakimi.core import bot_identity
from hakimi import paths
from hakimi.bridge.turn_lanes import active_context_path

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from hakimi.tools.bot_tools import ToolError, parse_input, require_context, run_tool
from hakimi.bridge.reply_format import text_bubbles
from hakimi.tools.sticker_policy import evaluate_sticker
from hakimi.tools.sticker_catalog import require_allowed, available_stickers, StickerPolicyError
from hakimi.bridge.quote_policy import allow_quote
from hakimi.bridge.turn_coordinator import TurnCoordinator, CoordinatorError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = paths.base()
STATE_DIR = BASE_DIR / "state"
STICKER_DIR = BASE_DIR / "stickers"
COLLECTED_DIR = STICKER_DIR / "collected"
SESSION_MEMORY_FILE = STATE_DIR / "session_memory.jsonl"
APPROVAL_FILE = STATE_DIR / "approval_requests.jsonl"

OUTBOX_FILE = STATE_DIR / "outbox.jsonl"
SIM_INPUT_FILE = STATE_DIR / "simulate_in.jsonl"
HTTP_API = "http://127.0.0.1:3000"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
REVIEW_CALLS = {"memory.read", "memory.search", "memory.graph", "memory.social",
                "memory.social_append", "reply.skip", "turn.failure"}


def _review_context(context):
    return context.get("internal_review") if isinstance(context.get("internal_review"), dict) else None


def _deny_review_output(context, action):
    if _review_context(context):
        raise ToolError(f"内部记忆反思不允许{action}；只可读记忆、写memory.social并reply.skip")


def _memory_ids(value):
    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = re.split(r"[,，\s]+", str(value or "").strip())
    result = []
    for part in parts:
        text = str(part).strip().upper().removeprefix("M")
        if text.isdigit():
            result.append("M" + text)
    return list(dict.fromkeys(result))


def _auto_session_evidence(base, session, target_id, payload=None):
    """Auto-bind recent visible chat M ids in the current session when scoring without explicit M ids."""
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(target_id or "")):
        return []
    try:
        from hakimi.memory.memory_lifecycle import _evidence_rows
        from hakimi.core.context_window import select_context
        rows, target = select_context(payload or {})
        found = _evidence_rows(base, session, rows, {"user_id": str(target_id), "message_id": target.get("message_id", "")}, time.time())
        if found:
            return [item["memory_id"] for item in found[:4]]
    except Exception:
        pass
    try:
        from hakimi.memory.memory_store import store
        with store(str(base)).db() as db:
            rows = db.execute(
                "SELECT id FROM records WHERE kind='chat' AND state NOT IN ('deleted','retracted') "
                "AND session=? AND user_id=? AND trim(COALESCE(text,''))!='' "
                "ORDER BY at DESC,id DESC LIMIT 4",
                (str(session), str(target_id)),
            ).fetchall()
        return ["M" + str(int(row["id"])) for row in rows]
    except Exception:
        return []


def _active_internal_review():
    try:
        context = json.loads(active_context_path(BASE_DIR).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(context, dict) and isinstance(context.get("internal_review"), dict)
            and float(context.get("expires_at", 0) or 0) > time.time())


def current_context(session, token=None):
    context = require_context(session, BASE_DIR)
    if context.get("owner_token"):
        if token != context["owner_token"] or context.get("provider") not in {"gemini","dsh"}:
            raise ToolError("必须使用本轮提示给出的 --turn；不能接管其他任务令牌")
        valid=TurnCoordinator(BASE_DIR).check_token(token,"dsh") if context["provider"]=="dsh" else TurnCoordinator(BASE_DIR).record_progress(token)
        if not valid:
            raise ToolError("本轮已完成、超时或被备用后端接管，丢弃迟到结果")
    return context


def _contained_path(path, root):
    root = Path(root)
    if root.resolve() != root or root.is_symlink():
        raise ToolError("白名单目录被重定向")
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(root):
        raise ToolError("只能访问白名单目录内的图片")
    return resolved


def _image_bytes(path):
    if path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
        raise ToolError("只允许 jpg/jpeg/png/gif/webp 图片文件")
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ToolError("图片大小须在 1 字节到 8 MiB 之间")
    # Verify content, not merely the extension. PIL is already a bridge dependency.
    import io
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "GIF", "WEBP"} or image.width * image.height > 20_000_000:
                raise ToolError("图片格式或尺寸超限")
            image.verify()
    except Exception as exc:
        raise ToolError("文件不是可验证的受支持图片") from exc
    return data


def _safe_sticker_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(?:jpg|jpeg|png|gif|webp)", name, re.IGNORECASE):
        raise ToolError("表情名须为英文/数字/下划线/短横线加受支持图片后缀")
    if Path(name).stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        raise ToolError("表情名不可为系统保留名称")
    return name


def _escape_cq_text(text):
    return text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")


def onebot_post(endpoint: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{HTTP_API}/{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_sticker_cq(sticker_name: str) -> str | None:
    safe_name = _safe_sticker_name(sticker_name)
    candidates = [(STICKER_DIR, safe_name), (COLLECTED_DIR, safe_name),
                  (COLLECTED_DIR, Path(safe_name).with_suffix(".gif").name),
                  (COLLECTED_DIR, Path(safe_name).with_suffix(".jpg").name)]
    for root, name in candidates:
        path = _contained_path(root / name, root)
        if path.exists():
            data = _image_bytes(path)
            require_allowed(data, path.name, BASE_DIR)
            return f"[CQ:image,file={path.as_uri()},sub_type=1]"
    raise ToolError("表情包不存在")


def cmd_groups(_args):
    resp = onebot_post("get_group_list", {})
    groups = resp.get("data", [])
    print(f"=== 当前已加入的 QQ 群聊 (共 {len(groups)} 个) ===")
    for g in groups:
        print(f"群号: {g['group_id']:<12} | 人数: {g['member_count']:<5} | 群名: {g['group_name']}")


def cmd_history(args):
    # Legacy syntax remains, but no longer permits arbitrary cross-group API reads.
    session = args.session or f"group:{args.group}"
    context = current_context(session, getattr(args, "turn", None))
    _deny_review_output(context, "读取聊天历史")
    if args.group and session != f"group:{args.group}":
        raise ToolError("group 与 session 不一致")
    result = run_tool("context", session, {"limit": args.count}, BASE_DIR)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_tool(args):
    context = current_context(args.session, getattr(args, "turn", None))
    _deny_review_output(context, "运行该工具")
    data = parse_input(args.input)
    if getattr(args, "url", None):
        if args.name != "bilibili_video":
            raise ToolError("--url 仅用于本轮B站视频工具")
        data["url"] = args.url
    result = run_tool(args.name, args.session, data, BASE_DIR)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_reply(args):
    try:
        context = current_context(args.session, getattr(args, "turn", None))
    except ToolError:
        if isinstance(args.text,str) and args.text.strip() and len(args.text)<=12000:
            from hakimi.models.provider_health import ProviderHealth
            try:ProviderHealth(BASE_DIR).late_reply(getattr(args,"turn",None),args.session)
            except (OSError,sqlite3.Error):pass
        raise
    _deny_review_output(context, "发送QQ消息")
    if not context.get("owner_token"):
        raise ToolError("旧版会话无任务令牌，请使用新版等待器领取任务")
    from hakimi.bridge.reply_format import MAX_REPLY_SEGMENTS
    from hakimi.tools.math_render import prepare_text,submit_text,wait_sent
    frame = None
    if getattr(args, 'video_frame', None) == 'F1':
        from hakimi.transport.image_permissions import video_frame_for_turn
        path = video_frame_for_turn(args.session, context['owner_token'], BASE_DIR)
        if path:
            frame = f'[CQ:image,file={path.as_uri()},sub_type=0]'
    if prepare_text(args.text):
        extra=[frame] if frame else []
        if args.sticker and evaluate_sticker(args.session,text=args.text,base=BASE_DIR)['allowed']:
            try:extra.append(resolve_sticker_cq(args.sticker))
            except (ToolError,StickerPolicyError):pass
        try:
            result=submit_text(TurnCoordinator(BASE_DIR),context['owner_token'],'gemini',args.text,BASE_DIR,
                               extra=[x for x in extra if x],files=getattr(args,'files',None),
                               reply_to=args.reply_to or None,interaction=getattr(args,'interaction',None))
            if not result['accepted']:raise ValueError(result['reason'])
            wait_sent(BASE_DIR,result['reply_id'],timeout=result['wait_seconds'])
        except ValueError as exc:raise ToolError(str(exc)) from exc
        print('成功')
        return
    try:
        segments = text_bubbles(args.text)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if args.reply_to and not re.fullmatch(r"-?[0-9]{1,20}", args.reply_to):
        raise ToolError("reply_to 必须是数字消息 ID")
    if args.reply_to and segments and allow_quote(args.session, args.reply_to, base=BASE_DIR):
        segments[0] = f"[CQ:reply,id={args.reply_to}]{segments[0]}"
    if frame and len(segments) < MAX_REPLY_SEGMENTS:
        segments.append(frame)
    if args.sticker:
        decision = evaluate_sticker(args.session, text=args.text, base=BASE_DIR)
        if len(segments) < MAX_REPLY_SEGMENTS and decision["allowed"]:
            try:
                cq = resolve_sticker_cq(args.sticker)
                if cq:
                    segments.append(cq)  # Images always have their own message.
            except (ToolError, StickerPolicyError):
                print("图片被禁用或不可用，本轮省略；不要改名或补发。")
        else:
            print("本轮保留正文，省略表情包；不要另开消息补发。")

    files = getattr(args, "files", None)
    if not segments and not files:
        if not TurnCoordinator(BASE_DIR).complete(context["owner_token"], "no_reply"):
            raise ToolError("本轮无法静默完成，可能已被接管")
        print("本轮无可发送内容，已静默完成。")
        return

    from hakimi.bridge.reply_format import MAX_SEGMENT_CHARS
    if any(len(segment) > MAX_SEGMENT_CHARS for segment in segments):
        raise ToolError("转义后消息超过发送上限，请精简正文")
    result = TurnCoordinator(BASE_DIR).submit_reply(context["owner_token"], segments, "gemini", files=files, reply_to=args.reply_to,
                                                  interaction=getattr(args,'interaction',None))
    if not result["accepted"]:
        raise ToolError("回复未提交：" + result["reason"])
    print(f"已写入发送队列 ({len(segments)+len(files or [])} 段消息)，等待平台回执")


def cmd_skip(args):
    context = current_context(args.session, args.turn)
    if not context.get("owner_token") or not TurnCoordinator(BASE_DIR).complete(context["owner_token"], "no_reply"):
        raise ToolError("本轮无法完成，可能已被接管")
    review = _review_context(context)
    if review:
        try:
            from hakimi.memory.personal_review import mark_completed
            mark_completed(BASE_DIR, review.get("review_id", ""))
        except (OSError, ValueError):
            pass
    print("本轮已静默完成")


def cmd_report_failure(args):
    context = current_context(args.session, args.turn)
    if not context.get("owner_token") or not TurnCoordinator(BASE_DIR).report_failure(context["owner_token"], args.reason):
        raise ToolError("本轮无法登记错误，可能已被接管")
    print("已登记明确错误，备用监控将检查接管")


def cmd_progress(args):
    context = current_context(args.session, args.turn)
    if not context.get("owner_token"):
        raise ToolError("进展登记必须使用本轮任务令牌")
    print("已登记本轮真实处理进展；总处理期限不会延长")


def cmd_save_sticker(args):
    context = current_context(args.session, getattr(args, "turn", None))
    _deny_review_output(context, "收藏图片")
    if context.get("provider")=="dsh":raise ToolError("dsh接管期间不收藏表情包")
    safe_name = _safe_sticker_name(args.name)
    raw_url = str(args.url).strip()
    if "://" in raw_url or raw_url.startswith(("\\\\", "//")):
        raise ToolError("收藏仅接受本地 img_cache 图片，不接受网络 URL")
    cache_root = STATE_DIR / "img_cache"
    # Only the daemon's structured cache collector may populate this lease field.
    # A filename or local path appearing in chat text never authorizes an image.
    raw_allowed = context.get("allowed_image_paths", [])
    if not isinstance(raw_allowed, list) or len(raw_allowed) > 64 or any(not isinstance(p, str) for p in raw_allowed):
        raise ToolError("当前会话图片授权清单无效")
    allowed_list = []
    for item in raw_allowed:
        if "://" in item or item.startswith(("\\\\", "//")):
            raise ToolError("当前会话图片授权路径无效")
        allowed_list.append(_contained_path(Path(item), cache_root))
    allowed = set(allowed_list)
    alias_match = re.fullmatch(r"I([1-9][0-9]*)", raw_url, re.I)
    if alias_match:
        idx = int(alias_match.group(1)) - 1
        visible_allowed = allowed_list[-8:]
        if 0 <= idx < len(visible_allowed):
            src_path = visible_allowed[idx]
        else:
            raise ToolError("图片编号超出本轮可见缓存清单范围")
    else:
        by_name = [p for p in allowed_list if p.name == raw_url]
        if len(by_name) == 1 and "/" not in raw_url and "\\" not in raw_url:
            src_path = by_name[0]
        else:
            src_path = _contained_path(Path(raw_url), cache_root)
    if src_path not in allowed:
        raise ToolError("图片不在本轮可信缓存清单中；聊天正文里的路径不能授予访问权限")
    gif_sibling = _contained_path(src_path.with_suffix(".gif"), cache_root)
    if gif_sibling in allowed and gif_sibling.is_file():
        src_path = gif_sibling
        safe_name = Path(safe_name).with_suffix(".gif").name
    data = _image_bytes(src_path)
    try:
        require_allowed(data, safe_name, BASE_DIR)
    except StickerPolicyError as exc:
        raise ToolError(str(exc) + " 禁图不能改名重试；其他表情照常收藏。") from exc
    target = _contained_path(COLLECTED_DIR / safe_name, COLLECTED_DIR)
    STICKER_DIR.mkdir(exist_ok=True)
    COLLECTED_DIR.mkdir(exist_ok=True)
    target.write_bytes(data)
    print(f"已收藏图片 {safe_name} ({len(data)} bytes)")


def cmd_mem_append(args):
    """Save a manually authored note through the same validated writer as dsh.

    The note is passed through byte-for-byte: no strip/truncate/rewrite.  Invalid
    related ids or over-long input are rejected before any original is stored.
    """
    from hakimi.memory.text import valid_source; from hakimi.memory.smart_memory import remember_memory, valid_memory

    context = current_context(args.session, getattr(args, "turn", None))
    _deny_review_output(context, "写memory.remember；内部反思只允许memory.social")
    from hakimi.voice.store import has_voice,scoped_saved_receipt
    if has_voice(BASE_DIR,TurnCoordinator(BASE_DIR).snapshot().get('payload',{})):
        print(json.dumps(scoped_saved_receipt(),ensure_ascii=False));return
    note = args.note
    if not isinstance(note, str) or not note.strip():
        raise ToolError("记忆正文不能为空；空白会被拒绝，原文会逐字符保存")
    kind = getattr(args, "kind", "preference")
    if kind not in ("person", "preference", "lore", "episode", "reflection", "commitment"):
        raise ToolError("记忆类型仅支持 person、preference、lore、episode、reflection、commitment")
    source = getattr(args, "source", "") or ""
    if kind == "lore":
        if not valid_source(source):
            raise ToolError("考据笔记须带 --source：完整 HTTPS 出处，最多400字，禁止账号信息或控制字符")
    elif source:
        raise ToolError("--source 仅用于 --kind lore 考据笔记")
    topics = getattr(args, "topics", "") or ""
    related = getattr(args, "related_memory_ids", "") or ""
    user_id = getattr(args, "user_id", "") or ""
    if kind != "person":
        user_id = ""
    proposal = {"kind": kind, "note": note, "source": source, "key": getattr(args, "key", "") or "",
                "evidence": getattr(args, "evidence", "") or "", "topics": topics,
                "related_memory_ids": related, "user_id": user_id}
    if not valid_memory(proposal):
        if kind == "person" and not user_id:
            raise ToolError("person 笔记须给出本轮可见成员的官方 --user-id")
        if kind == "person":
            raise ToolError("人物笔记参数无效；user_id须为本轮可见成员，key最多64字")
        raise ToolError("笔记参数无效；检查 key、evidence、topics、related_memory_ids 的格式与长度")
    from hakimi.core.context_window import select_context
    payload = TurnCoordinator(BASE_DIR).snapshot().get("payload", {})
    origin = select_context(payload)[1].get("message_id", "") if payload else ""
    # A stable turn token is the fallback origin when no single message id exists;
    # retries of the same tool turn then resolve to the same request id.
    origin = str(origin or getattr(args, "turn", "") or "")
    result = remember_memory(BASE_DIR, args.session, proposal, context.get("visible_users", {}),
                             provider=context.get("provider","gemini"), channel=context.get("provider","gemini")+"_tool",
                             origin_message_id=origin)
    if not result.get("saved"):
        error = result.get("error")
        if error == "person_not_visible":
            raise ToolError("人物笔记须对应本轮可见成员；不能凭昵称或自由姓名合并身份")
        if error == "related_memory_unreadable":
            raise ToolError("related_memory_ids 含当前不可读或不存在的M编号；未保存")
        raise ToolError("记忆未保存：参数无效")
    # This receipt is the only evidence that the tool actually wrote the note.
    print(json.dumps(result, ensure_ascii=False))


def cmd_call(args):
    from hakimi.tools.math_render import log_tool_call
    from hakimi.core.runtime_io import read_json_cached
    provider=read_json_cached(active_context_path(BASE_DIR),{}).get('provider','gemini')
    started=time.monotonic()
    from hakimi.memory.memory_usage import record as memory_receipt
    # The payload is diagnostic input only; tool authority is checked separately.
    try:
        payload=TurnCoordinator(BASE_DIR).snapshot().get("payload",{}) if args.call_name.startswith("memory.") else {}
    except Exception:
        payload={}
    tool_coordinator=TurnCoordinator(BASE_DIR)
    tool_token=getattr(args,'turn',None)
    tool_active=provider=='gemini' and tool_coordinator.tool_activity(tool_token,args.call_name)
    try:
        result=_cmd_call(args)
    except Exception as exc:
        memory_receipt(BASE_DIR,args.call_name,payload,error=exc,provider=provider)
        log_tool_call(BASE_DIR,args.call_name,'error',provider=provider,error_type=type(exc).__name__)
        raise
    finally:
        if tool_active:tool_coordinator.tool_activity(tool_token)
    memory_receipt(BASE_DIR,args.call_name,payload,output=result,provider=provider)
    log_tool_call(BASE_DIR,args.call_name,'ok',provider=provider,elapsed_ms=round((time.monotonic()-started)*1000))
    return result


def _cmd_call(args):
    """Thin aliases over existing implementations; no new execution authority."""
    import contextlib
    import io
    from types import SimpleNamespace
    from hakimi.prompt.tool_catalog import BY_NAME, describe
    name = args.call_name
    if name not in BY_NAME:
        raise ToolError("未知工具名")
    try:context = current_context(args.session, args.turn)
    except ToolError:
        if name in {"reply.send", "files.send", "chat.export", "math.send"}:
            from hakimi.models.provider_health import ProviderHealth
            try:ProviderHealth(BASE_DIR).late_reply(args.turn, args.session)
            except (OSError, sqlite3.Error):pass
        raise
    review = _review_context(context)
    if review and name not in REVIEW_CALLS:
        raise ToolError("内部记忆反思只允许memory.read/search/graph/social与reply.skip；禁止发送消息、文件、公式或运行其他工具")
    props = BY_NAME[name]["inputSchema"]["properties"]
    data = {key: getattr(args, key) for key in props if getattr(args, key, None) is not None}
    if name in {'memory.social','memory.social_append'}:
        from hakimi.voice.store import has_voice,scoped_saved_receipt
        if has_voice(BASE_DIR,TurnCoordinator(BASE_DIR).snapshot().get('payload',{})):
            print(json.dumps(scoped_saved_receipt(),ensure_ascii=False));return
    if name in {'voice.speak','voice.hum'}:
        from hakimi.voice.tts import speak
        coordinator=TurnCoordinator(BASE_DIR);claim=coordinator.snapshot();claim['token']=args.turn
        print(json.dumps(speak(BASE_DIR,claim,coordinator,'gemini',data.get('text'),hum=name=='voice.hum'),ensure_ascii=False));return
    if name == 'video.watch_attachment':
        from hakimi.tools.qq_video import watch
        coordinator = TurnCoordinator(BASE_DIR)
        state = coordinator.snapshot()
        if state.get('owner_token') != args.turn:
            raise ToolError('本轮已过期')
        report = watch(BASE_DIR, state['payload'], data.get('ref'), owner=args.turn,
                       is_active=lambda:coordinator.check_token(args.turn, 'gemini'))
        report.pop('_frame_path', None)
        print(json.dumps(report,ensure_ascii=False))
        return
    if name == 'members.mention':
        from hakimi.transport.member_mentions import mention
        print(json.dumps(mention(BASE_DIR,TurnCoordinator(BASE_DIR),args.turn,'gemini',**data),ensure_ascii=False))
        return
    if name == 'diary.read':
        from hakimi.features.diary_store import read_entry,day_value
        row=read_entry(BASE_DIR,data.get('day'))
        print(json.dumps({k:row[k] for k in ('day','title','text','image_prompt')},ensure_ascii=False))
        return
    if name == 'diary.export':
        from hakimi.features.diary_store import submit_export,wait_sent
        try:
            result=submit_export(TurnCoordinator(BASE_DIR),args.turn,'gemini',data,BASE_DIR)
            if not result['accepted']:raise ValueError(result['reason'])
            wait_sent(BASE_DIR,result['reply_id'])
        except (ValueError,RuntimeError) as exc:raise ToolError(str(exc)) from exc
        print('成功')
        return
    if name == 'math.send':
        from hakimi.tools.math_render import submit, wait_sent
        try:
            result=submit(TurnCoordinator(BASE_DIR),args.turn,'gemini',data,BASE_DIR)
            if not result['accepted']:raise ValueError('公式未提交：'+result['reason'])
            wait_sent(BASE_DIR,result['reply_id'],timeout=result['wait_seconds'])
        except ValueError as exc:raise ToolError(str(exc)) from exc
        print('成功')
        return
    if name == "tools.list":
        try:
            output = describe(data.get("tool_name"))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
    elif name in {"web.search", "web.read","web.sources"}:
        from hakimi.models.web_access import run
        payload=TurnCoordinator(BASE_DIR).snapshot().get('payload',{})
        if payload.get('session_key')!=args.session:raise ToolError('搜索会话不匹配')
        output = run(name, data, 15,BASE_DIR,payload)
    elif name == "images.list":
        output = {"images": context.get("allowed_image_paths", [])}
    elif name == "files.list":
        from hakimi.transport.file_delivery import directory, menu
        output = {"files": menu(args.session, BASE_DIR, limit=64), "output_directory": str(directory(args.session, BASE_DIR))}
    elif name == "stickers.list":
        from hakimi.models.fallback_style import sticker_menu
        output = {"stickers": sticker_menu(TurnCoordinator(BASE_DIR).snapshot().get("payload", {}), BASE_DIR)}
    elif name == "voice.read":
        from hakimi.voice.store import read as read_voice
        output=read_voice(BASE_DIR,args.session,data["message_id"],data.get("offset",0),data.get("limit",3000))
        if output.get("text"):
            from hakimi.voice.store import mark_turn
            mark_turn(BASE_DIR,args.session,TurnCoordinator(BASE_DIR).snapshot().get("trigger_id",""))
    elif name == "memory.read":
        from hakimi.memory.memory_store import store
        from hakimi.tools.memory_result import memory_read_result
        record = store(str(BASE_DIR)).read(
            data["memory_id"], args.session, context.get("visible_users", {}),
            cross_groups=True, offset=data.get("offset", 0), limit=data.get("limit", 3000),
            source=data.get("source", "note"))
        if record is None:
            raise ToolError("该记忆不存在或不属于当前可读范围")
        if record.get('channel')=='voice':
            from hakimi.voice.store import mark_turn
            mark_turn(BASE_DIR,args.session,TurnCoordinator(BASE_DIR).snapshot().get('trigger_id',''))
        output = memory_read_result(record)
    elif name == "memory.search":
        from hakimi.memory.memory_store import store
        from hakimi.core.context_window import select_context
        payload = TurnCoordinator(BASE_DIR).snapshot().get("payload", {})
        if not payload:
            payload = {"session_key": args.session, "recent_context_messages": []}
        rows,target=select_context(payload)
        cross=data.get('scope','current')=='all_groups'
        retrieval_limit = max(1, min(12, int(data.get('limit') or 6)))
        output = {"scope":'all_groups' if cross else 'current',"memories":store(str(BASE_DIR)).retrieve(
            args.session,rows,target,query=str(data.get('query') or target.get('text',''))[:300],
            cross_groups=cross,budget=retrieval_limit * 200,limit=retrieval_limit,explain=True)}
        if any('[语音:' in line for line in output['memories']):
            from hakimi.voice.store import mark_turn
            mark_turn(BASE_DIR,args.session,payload.get('trigger_id',''))
    elif name == "memory.graph":
        from hakimi.memory.memory_store import store
        from hakimi.memory.memory_graph import compact_graph
        cross = data.get('scope', 'current') == 'all_groups'
        focus_type = 'person' if data.get('person_id') else 'topic' if data.get('topic') else 'memory' if data.get('memory_id') else None
        focus_id = data.get('person_id') or data.get('memory_id') or None
        output = store(str(BASE_DIR)).graph(
            args.session, context.get("visible_users", {}), cross_groups=cross,
            focus_type=focus_type, focus_id=focus_id, topic=data.get('topic'),
            depth=data.get('depth', 1), limit=data.get('limit', 24),
            evidence_limit=2)
        output = compact_graph(output)
    elif name in {"memory.social", "memory.social_append"}:
        from hakimi.social.social_record import record_social; from hakimi.social.social_fields import SocialError
        from hakimi.core.context_window import select_context
        author_provider = context.get("provider", "gemini")
        if author_provider not in {"gemini", "dsh"}:
            raise ToolError("当前后端尚未支持关系记忆来源标注")
        note = data.get("note")
        target_id = str(data.get("target_id") or "")
        subject_id = data.get("subject_id") or "hakimi"
        mode = "append" if name == "memory.social_append" else (data.get("mode") or "replace")
        has_score = any(data.get(k) is not None for k in ("affinity", "closeness", "confidence"))
        evidence_input = data.get("evidence_memory_ids") or []
        payload = TurnCoordinator(BASE_DIR).snapshot().get("payload", {})
        if review:
            candidates = {str(item.get("person_id", "")): item for item in review.get("candidates", [])
                          if isinstance(item, dict)}
            if target_id not in candidates:
                raise ToolError("内部反思只能评价本轮候选人物")
            candidate_evidence = {str(value) for value in
                                  candidates[target_id].get("evidence_memory_ids", [])}
            selected_evidence = _memory_ids(evidence_input)
            if selected_evidence and any(value not in candidate_evidence
                                         for value in selected_evidence):
                raise ToolError("内部反思只能引用本轮为该人物提供的证据M编号")
            if not selected_evidence and has_score and subject_id == "hakimi":
                selected_evidence = [str(value) for value in
                                     candidates[target_id].get("evidence_memory_ids", [])][:8]
            # A candidate may receive a pure-text subjective note without evidence;
            # record_social still enforces the Gemini/subject/no-score boundary.
            evidence_input = selected_evidence
        elif not _memory_ids(evidence_input) and has_score and subject_id == "hakimi":
            evidence_input = _auto_session_evidence(BASE_DIR, args.session, target_id, payload)
        origin = select_context(payload)[1].get("message_id", "") if payload else ""
        origin = str(origin or args.turn or "")
        if not origin and review:
            origin = "review:" + str(review.get("review_id", ""))
        try:
            output = record_social(
                BASE_DIR, args.session, note, target_id,
                subject_id=subject_id,
                relationship_type=data.get("relationship_type") or "",
                affinity=data.get("affinity"), closeness=data.get("closeness"),
                confidence=data.get("confidence"),
                evidence_memory_ids=evidence_input,
                provider=author_provider,
                provenance="dsh_assisted" if author_provider == "dsh" else "gemini_manual",
                origin_message_id=origin,
                reason=data.get('reason') or '',
                impact=data.get('impact') or 'ordinary',
                mode=mode,
            )
        except SocialError as exc:
            raise ToolError(exc.message) from exc
        if review:
            try:
                from hakimi.memory.personal_review import mark_written
                mark_written(BASE_DIR, review.get("review_id", ""), target_id, output)
            except (OSError, ValueError):
                pass
    else:
        aliases = {"time.now": "time", "runtime.status": "status", "calculator.evaluate": "calculator", "context.read": "context", "video.watch": "bilibili_video", "video.watch_miniapp": "qq_miniapp_video"}
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            if name in aliases:
                output = run_tool(aliases[name], args.session, data, BASE_DIR)
                print(json.dumps(output, ensure_ascii=False))
            else:
                common = {"session": args.session, "turn": args.turn}
                if name == "reply.send":
                    cmd_reply(SimpleNamespace(**common, text=data.get("text", ""), sticker=data.get("sticker", ""),
                        reply_to=data.get("reply_to", ""), interaction=data.get('interaction'),
                        video_frame=data.get('video_frame')))
                elif name in {"files.send", "chat.export"}:
                    spec = {key: value for key,value in data.items() if key != "text"}
                    if name == "chat.export":spec.update(export=True, name=data.get("name", "聊天摘录.txt"))
                    cmd_reply(SimpleNamespace(**common, text=data.get("text", ""), sticker="", reply_to="", files=[spec]))
                elif name == "reply.skip":
                    cmd_skip(SimpleNamespace(**common))
                elif name == "turn.progress":
                    cmd_progress(SimpleNamespace(**common))
                elif name == "turn.failure":
                    cmd_report_failure(SimpleNamespace(**common, reason=data["reason"]))
                elif name == "memory.remember":
                    cmd_mem_append(SimpleNamespace(**common, kind=data.get("kind", "preference"), note=data["note"],
                        source=data.get("source", ""), user_id=data.get("user_id", ""), key=data.get("key", ""),
                        evidence=data.get('evidence',''), topics=data.get('topics',''),
                        related_memory_ids=data.get('related_memory_ids','')))
                elif name == "stickers.collect":
                    cmd_save_sticker(SimpleNamespace(**common, url=data["image"], name=data["filename"]))
        text = captured.getvalue().strip()
        try:
            output = json.loads(text)
        except ValueError:
            output = {"message": text}
    print(json.dumps({"tool": name, "ok": True, "result": output}, ensure_ascii=False))
    return output


def cmd_request_approval(args):
    """当 QQ 用户请求写程序、跑命令或读写非白名单文档时，写入待审核队列"""
    record = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "PENDING_REVIEW",
        "session_key": args.session,
        "user_id": args.user,
        "nickname": args.nick,
        "request_summary": args.summary,
    }
    STATE_DIR.mkdir(exist_ok=True)
    with open(APPROVAL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"【安全审核拦截】已将请求登记至待审核队列 state/approval_requests.jsonl: {record}")


def cmd_stickers(_args):
    print("=== 当前可用表情（不代表本轮需要贴图） ===")
    for name in available_stickers(BASE_DIR):
        print("  - " + Path(name).name)


def cmd_sim(args):
    event = {
        "post_type": "message",
        "message_type": "group" if args.group else "private",
        "self_id": bot_identity.SELF_QQ,
        "user_id": int(args.user),
        "group_id": int(args.group) if args.group else None,
        "raw_message": args.text,
        "sender": {"user_id": int(args.user), "nickname": args.nick},
    }
    STATE_DIR.mkdir(exist_ok=True)
    with open(SIM_INPUT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(f"已注入模拟 QQ 消息 -> [{args.nick}({args.user})]: {args.text}")


def cmd_cleanup(args):
    from hakimi.bridge.bridge_daemon import perform_periodic_cleanup
    res = perform_periodic_cleanup(
        img_max_age_sec=args.img_age,
        ntqq_pic_max_age_sec=args.ntqq_age,
        max_history_lines=args.keep_history,
        max_outbox_lines=args.keep_outbox,
    )
    print(f"手动清理完成: {json.dumps(res, ensure_ascii=False)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    from hakimi.prompt.tool_catalog import register_cli
    register_cli(sub, cmd_call)

    sub.add_parser("groups")
    sub.add_parser("stickers")

    p_clean = sub.add_parser("cleanup")
    p_clean.add_argument("--img_age", type=int, default=1800, help="state/img_cache 图片保留秒数 (默认1800秒=30分钟)")
    p_clean.add_argument("--ntqq_age", type=int, default=2700, help="NTQQ 底层图片缓存保留秒数 (默认2700秒=45分钟)")
    p_clean.add_argument("--keep_history", type=int, default=150, help="chat_history.jsonl 保留条数")
    p_clean.add_argument("--keep_outbox", type=int, default=50, help="outbox.jsonl 保留条数")

    p_hist = sub.add_parser("history")
    p_hist.add_argument("--group", default="")
    p_hist.add_argument("--session", default="")
    p_hist.add_argument("--turn", default="")
    p_hist.add_argument("--count", type=int, default=25)

    p_tool = sub.add_parser("tool", help="当前会话内的只读受限工具")
    p_tool.add_argument("--session", required=True)
    p_tool.add_argument("--turn", default="")
    p_tool.add_argument("--name", choices=("calculator", "context", "status", "time", "bilibili_video", "qq_miniapp_video"), required=True)
    p_tool.add_argument("--input", default="{}", help="严格 JSON 对象，不接受路径/URL/命令")
    p_tool.add_argument("--url", help="B站视频链接或BV号，免去JSON转义")

    p_reply = sub.add_parser("reply")
    p_reply.add_argument("--session", required=True)
    p_reply.add_argument("--turn", required=True)
    p_reply.add_argument("--text", default="", help="用 | 或换行按语义分条；长文弹性拆分")
    p_reply.add_argument("--reply_to", default="", help="可选：引用回复某条消息的 message_id")
    p_reply.add_argument("--sticker", default="", help="可选：现有表情，独立一条；本地策略可省略")

    p_save = sub.add_parser("save_sticker")
    p_save.add_argument("--session", required=True)
    p_save.add_argument("--turn", default="")
    p_save.add_argument("--url", required=True, help="仅本地 state/img_cache 图片路径，禁止 URL")
    p_save.add_argument("--name", required=True)

    p_mem = sub.add_parser("mem_append")
    p_mem.add_argument("--session", required=True)
    p_mem.add_argument("--turn", default="")
    p_mem.add_argument("--kind", choices=("person", "preference", "lore", "episode", "reflection", "commitment"), default="preference", help="person跨群人物；lore带出处；episode经历；reflection主观反思；commitment约定")
    p_mem.add_argument("--user-id", default="", help="person：本轮可见成员官方ID")
    p_mem.add_argument("--key", default="", help="稳定事项名；同一事项以新记录为准")
    p_mem.add_argument("--source", default="", help="lore 必填：完整 HTTPS 出处，最多400字")
    p_mem.add_argument("--evidence", default="", help="支持笔记的短原文，最多240字")
    p_mem.add_argument("--topics", default="", help="可选：逗号分隔话题标签，最多8个")
    p_mem.add_argument("--related-memory-ids", dest="related_memory_ids", default="", help="可选：逗号分隔的M编号")
    p_mem.add_argument("--note", required=True, help="原话笔记；逐字符保存，不改写")

    p_app = sub.add_parser("request_approval")
    p_app.add_argument("--session", required=True)
    p_app.add_argument("--user", required=True)
    p_app.add_argument("--nick", default="")
    p_app.add_argument("--summary", required=True, help="请求写代码/改文档的具体内容摘要")

    for name in ("skip", "report_failure", "progress"):
        command = sub.add_parser(name)
        command.add_argument("--session", required=True)
        command.add_argument("--turn", required=True)
        if name == "report_failure":
            command.add_argument("--reason", required=True, choices=("quota", "auth", "model_error", "unavailable"))

    p_sim = sub.add_parser("sim")
    p_sim.add_argument("--user", default="10001")
    p_sim.add_argument("--nick", default="测试好友A")
    p_sim.add_argument("--group", default="88888")
    p_sim.add_argument("--text", required=True)

    parsed = parser.parse_args()
    handlers = {"groups": cmd_groups, "stickers": cmd_stickers, "history": cmd_history,
                "reply": cmd_reply, "save_sticker": cmd_save_sticker, "mem_append": cmd_mem_append,
                "request_approval": cmd_request_approval, "sim": cmd_sim, "cleanup": cmd_cleanup,
                "tool": cmd_tool}
    handlers.update(skip=cmd_skip, report_failure=cmd_report_failure, progress=cmd_progress)
    handlers["call"] = cmd_call
    try:
        if _active_internal_review() and parsed.cmd not in {"call", "skip", "progress", "report_failure"}:
            raise ToolError("内部记忆反思只允许memory.read/search/graph/social与reply.skip；其他命令均被硬拦截")
        handlers[parsed.cmd](parsed)
    except (ToolError, OSError, CoordinatorError, StickerPolicyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
