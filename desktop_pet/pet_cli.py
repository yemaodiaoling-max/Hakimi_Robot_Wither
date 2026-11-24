"""CLI control interface for Antigravity AI Agent serving the Hakimi Desktop Pet.

Provides commands for replying to pending turns, rotating context summaries,
and executing server-side memory and diary tools directly over ServerBridge.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

CONTEXT_FILE = STATE_DIR / "pet_agy_context.json"
RESPONSE_FILE = STATE_DIR / "pet_turn_response.json"

from desktop_pet.pet_handoff import HANDOFF_FILE, write_handoff_summary

_KNOWN_PUBLIC_GROUP_KEYWORDS = (
    "主人子协会", "万有引力", "摸鱼部", "考据", "高数", "高等数学", "原神",
    "学习群", "测试群", "协会", "俱乐部", "交流群", "讨论群", "水群",
)
_PRIVATE_CHAT_TEXT_RE = re.compile(
    r"私聊|单聊|小窗|私信|微信聊|"
    r"(?:找|跟|和|与)[^，。|；！？\s]{1,12}(?:私聊|单聊|小窗|聊社科|聊哲学|聊起|聊到|私下聊|单独聊)|"
    r"抖落.*私聊"
)


def _is_private_chat_observation(window_title: str = "", owner_text: str = "", pet_text: str = "") -> bool:
    win = str(window_title or "").strip()
    owner = str(owner_text or "").strip()
    pet = str(pet_text or "").strip()
    if win:
        is_multi_im = bool(re.search(r"等\d+个会话", win)) or any(k in win for k in ("私聊", "单聊", "微信", "WeChat"))
        if is_multi_im and not any(gk in win for gk in _KNOWN_PUBLIC_GROUP_KEYWORDS):
            return True
    combined_text = f"{owner} {pet}"
    if _PRIVATE_CHAT_TEXT_RE.search(combined_text):
        return True
    return False


def _sanitize_summary_for_qq_sync(summary: str) -> str:
    raw = str(summary or "").strip()
    if not raw:
        return ""
    clauses = [c.strip() for c in re.split(r"(?<=[。；！？|\n])|(?:\s*[；|]\s*)", raw) if c and c.strip()]
    kept = [c for c in clauses if not _is_private_chat_observation("", "", c)]
    return "".join(kept).strip()


def load_agy_context() -> dict:
    if CONTEXT_FILE.exists():
        try:
            data = json.loads(CONTEXT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                recent_turns = data.get("recent_turns", [])
                last_pending = data.get("last_pending_turn")
                return {
                    "turns": int(data.get("turns", 0)),
                    "max_turns": int(data.get("max_turns", 10)),
                    "rolling_summary": str(data.get("rolling_summary", "")),
                    "total_rotations": int(data.get("total_rotations", 0)),
                    "recent_turns": recent_turns if isinstance(recent_turns, list) else [],
                    "last_pending_turn": last_pending if isinstance(last_pending, dict) else {},
                }
        except Exception:
            pass
    return {
        "turns": 0,
        "max_turns": 10,
        "rolling_summary": "",
        "total_rotations": 0,
        "recent_turns": [],
        "last_pending_turn": {},
    }


def save_agy_context(data: dict) -> None:
    tmp = CONTEXT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONTEXT_FILE)


def cmd_reply(args: argparse.Namespace) -> int:
    text = str(args.text or "").strip()
    turn_id = str(args.turn_id or "").strip()
    if not turn_id:
        print(json.dumps({"ok": False, "error": "missing_turn_id"}, ensure_ascii=False))
        return 1
    if not text:
        text = "喵？"

    bubbles = [b.strip() for b in re.split(r"\s*\|\s*", text) if b.strip()]
    if not bubbles:
        bubbles = [text]

    ctx = load_agy_context()
    ctx["turns"] = ctx.get("turns", 0) + 1
    save_agy_context(ctx)

    badge = args.badge or "⚡ Antigravity Hydrogen 直连"
    resp = {
        "ok": True,
        "turn_id": turn_id,
        "reply": text,
        "bubbles": bubbles,
        "sprite": args.sprite or "happy",
        "badge": badge,
        "model": "gemini-4-hydrogen",
        "context": {
            "turns": ctx["turns"],
            "max_turns": ctx["max_turns"],
            "rolling_summary": ctx["rolling_summary"],
            "total_rotations": ctx["total_rotations"],
        },
        "at": time.time(),
    }

    tmp_resp = RESPONSE_FILE.with_suffix(".tmp")
    tmp_resp.write_text(json.dumps(resp, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_resp, RESPONSE_FILE)

    last_pending = ctx.get("last_pending_turn") if isinstance(ctx.get("last_pending_turn"), dict) else {}
    win_title = str(last_pending.get("window_title", "") or "")
    owner_text = str(last_pending.get("text", "") or "")
    is_private = bool(getattr(args, "private", False)) or _is_private_chat_observation(win_title, owner_text, text)

    recent_turns = list(ctx.get("recent_turns") or [])
    recent_turns.append({
        "window": win_title,
        "owner": owner_text,
        "pet": text,
        "private": is_private,
        "at": time.time(),
    })
    ctx["recent_turns"] = recent_turns[-3:]
    save_agy_context(ctx)

    sync_res: dict = {}
    if is_private:
        sync_res = {"ok": True, "skipped": "private_chat"}
    else:
        safe_turns = [
            t for t in ctx["recent_turns"]
            if isinstance(t, dict)
            and not t.get("private")
            and not _is_private_chat_observation(str(t.get("window", "") or ""), str(t.get("owner", "") or ""), str(t.get("pet", "") or ""))
        ]
        safe_summary = _sanitize_summary_for_qq_sync(ctx.get("rolling_summary", ""))
        try:
            from desktop_pet.client_bridge import ServerBridge
            bridge = ServerBridge()
            try:
                sync_res = bridge.request({
                    "action": "sync_thought",
                    "window_title": win_title,
                    "owner_text": owner_text,
                    "reply_text": text,
                    "recent_turns": safe_turns,
                    "rolling_summary": safe_summary,
                    "is_rotate": False,
                })
            finally:
                bridge.close()
        except Exception:
            sync_res = {"ok": False}

    print(json.dumps({
        "ok": True,
        "turn_id": turn_id,
        "bubbles_count": len(bubbles),
        "turns": ctx["turns"],
        "max_turns": ctx["max_turns"],
        "should_rotate": ctx["turns"] >= ctx["max_turns"],
        "next_action": "ROTATE_AND_HANDOFF_NEW_SESSION" if ctx["turns"] >= ctx["max_turns"] else "WAIT_NEXT_TURN",
        "private_skipped": is_private,
        "thought_synced": bool(sync_res.get("ok")) and not is_private,
    }, ensure_ascii=False))
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    summary = _sanitize_summary_for_qq_sync(str(args.summary or "").strip())
    ctx = load_agy_context()
    ctx["turns"] = 0
    ctx["total_rotations"] = ctx.get("total_rotations", 0) + 1
    if summary:
        ctx["rolling_summary"] = summary
    save_agy_context(ctx)
    handoff_path = write_handoff_summary(
        ctx.get("rolling_summary", ""),
        ctx.get("total_rotations", 0),
        handoff_file=HANDOFF_FILE,
    )

    safe_turns = [
        t for t in (ctx.get("recent_turns") or [])
        if isinstance(t, dict)
        and not t.get("private")
        and not _is_private_chat_observation(str(t.get("window", "") or ""), str(t.get("owner", "") or ""), str(t.get("pet", "") or ""))
    ]
    sync_res: dict = {}
    try:
        from desktop_pet.client_bridge import ServerBridge
        bridge = ServerBridge()
        try:
            sync_res = bridge.request({
                "action": "sync_thought",
                "rolling_summary": _sanitize_summary_for_qq_sync(ctx.get("rolling_summary", "")),
                "recent_turns": safe_turns,
                "is_rotate": True,
            })
        finally:
            bridge.close()
    except Exception:
        sync_res = {"ok": False}

    print(json.dumps({
        "ok": True,
        "rotated": True,
        "total_rotations": ctx["total_rotations"],
        "rolling_summary": ctx["rolling_summary"],
        "handoff_file": str(handoff_path),
        "thought_synced": bool(sync_res.get("ok")),
    }, ensure_ascii=False))
    return 0


def cmd_handoff_sync(args: argparse.Namespace) -> int:
    ctx = load_agy_context()
    handoff_path = write_handoff_summary(
        ctx.get("rolling_summary", ""),
        ctx.get("total_rotations", 0),
        reset_rules=bool(getattr(args, "reset_rules", False)),
        handoff_file=HANDOFF_FILE,
    )
    print(json.dumps({
        "ok": True,
        "handoff_file": str(handoff_path),
        "total_rotations": ctx.get("total_rotations", 0),
        "rolling_summary": ctx.get("rolling_summary", ""),
    }, ensure_ascii=False))
    return 0


def cmd_mem_search(args: argparse.Namespace) -> int:
    from desktop_pet.client_bridge import ServerBridge
    bridge = ServerBridge()
    res = bridge.request({
        "action": "tool_exec",
        "tool_name": "memory_search",
        "args": {
            "query": args.query,
            "scope": args.scope or "all_groups",
        },
    })
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def cmd_mem_read(args: argparse.Namespace) -> int:
    from desktop_pet.client_bridge import ServerBridge
    bridge = ServerBridge()
    res = bridge.request({
        "action": "tool_exec",
        "tool_name": "memory_read",
        "args": {
            "memory_id": args.id,
            "offset": args.offset,
            "limit": args.limit,
        },
    })
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def cmd_mem_remember(args: argparse.Namespace) -> int:
    from desktop_pet.client_bridge import ServerBridge
    bridge = ServerBridge()
    res = bridge.request({
        "action": "tool_exec",
        "tool_name": "memory_remember",
        "args": {
            "note": args.note,
            "kind": args.kind,
            "key": args.key or "桌面陪伴记录",
            "evidence": args.evidence or "主人与桌宠哈基米对话",
            "topics": args.topics or "桌宠,主人",
        },
    })
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def cmd_diary_read(args: argparse.Namespace) -> int:
    from desktop_pet.client_bridge import ServerBridge
    bridge = ServerBridge()
    payload = {"action": "tool_exec", "tool_name": "diary_read", "args": {}}
    if args.day:
        payload["args"]["day"] = args.day
    res = bridge.request(payload)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def cmd_bili_status(args: argparse.Namespace) -> int:
    from desktop_pet.client_bridge import ServerBridge
    bridge = ServerBridge()
    try:
        res = bridge.request({
            "action": "tool_exec",
            "tool_name": "bili_status",
            "args": {"limit": max(1, min(20, int(args.limit)))},
        })
    finally:
        bridge.close()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Hakimi Desktop Pet CLI for Antigravity AI Agent")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # reply
    p_reply = subparsers.add_parser("reply", help="Reply to the pending desktop pet turn")
    p_reply.add_argument("--turn-id", required=True, help="Turn ID received from pet_waiter")
    p_reply.add_argument("--text", required=True, help="Reply text (use '|' to separate speech bubbles)")
    p_reply.add_argument("--sprite", default="happy", choices=["happy", "peek", "thinking", "idle"], help="Pet sprite expression")
    p_reply.add_argument("--badge", default=None, help="Optional badge on bubble (e.g. '⚡ Antigravity Hydrogen 直连')")
    p_reply.add_argument("--private", action="store_true", help="Mark turn as private chat screen observation (do not sync to QQ memory)")
    p_reply.set_defaults(func=cmd_reply)

    # rotate
    p_rotate = subparsers.add_parser("rotate", help="Rotate rolling context window and save summary")
    p_rotate.add_argument("--summary", default="", help="1~2 sentence relay summary for future turns")
    p_rotate.set_defaults(func=cmd_rotate)

    # handoff-sync
    p_hsync = subparsers.add_parser("handoff-sync", help="Sync rolling summary to fixed handoff markdown without rotating turns")
    p_hsync.add_argument("--reset-rules", action="store_true", help="Reset static rules section to default template")
    p_hsync.set_defaults(func=cmd_handoff_sync)

    # mem-search
    p_msearch = subparsers.add_parser("mem-search", help="Search server shared long-term memory")
    p_msearch.add_argument("--query", required=True, help="Keywords or topic")
    p_msearch.add_argument("--scope", default="all_groups", choices=["all_groups", "current"], help="Search scope")
    p_msearch.set_defaults(func=cmd_mem_search)

    # mem-read
    p_mread = subparsers.add_parser("mem-read", help="Read full memory record by ID")
    p_mread.add_argument("--id", required=True, help="Memory ID (e.g. M1234)")
    p_mread.add_argument("--offset", type=int, default=0, help="Character offset")
    p_mread.add_argument("--limit", type=int, default=2500, help="Character limit")
    p_mread.set_defaults(func=cmd_mem_read)

    # mem-remember
    p_mrem = subparsers.add_parser("mem-remember", help="Save fact, preference, or event to long-term memory")
    p_mrem.add_argument("--note", required=True, help="Memory content to save")
    p_mrem.add_argument("--kind", required=True, choices=["person", "preference", "lore", "episode", "reflection", "commitment"], help="Memory kind")
    p_mrem.add_argument("--key", default="桌面陪伴记录", help="Short topic/item key")
    p_mrem.add_argument("--evidence", default="主人与桌宠哈基米对话", help="Evidence quote or note")
    p_mrem.add_argument("--topics", default="桌宠,主人", help="Comma-separated topics")
    p_mrem.set_defaults(func=cmd_mem_remember)

    # diary-read
    p_dread = subparsers.add_parser("diary-read", help="Read Hakimi diary from server")
    p_dread.add_argument("--day", default=None, help="Date in YYYY-MM-DD format (omit for latest)")
    p_dread.set_defaults(func=cmd_diary_read)

    # bili-status
    p_bili = subparsers.add_parser("bili-status", help="Read Bilibili companion live presence and recent watched episodes")
    p_bili.add_argument("--limit", type=int, default=5, help="Max recent episodes (1..20)")
    p_bili.set_defaults(func=cmd_bili_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
