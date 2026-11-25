"""
QQ <-> Antigravity 桥接守护进程 (动态筛选 + 多模态图片缓存 + 持久发送检查点)

核心机制：
1. 【多模态图片自动落盘缓存与引用解析 (Image Cache & Reply Resolver)】：
   - 自动将群聊/私聊中的 `[CQ:image]` 通过 NapCat `get_image` 接口提取并转换为标准 RGB `.jpg` 存入白名单目录 `state/img_cache/`。
   - 自动解析 `[CQ:reply,id=...]` 引用消息（含被引用消息的文字或图片路径），让 `hakimi-flash` 直接用 `view_file` / `read_file` 即可看到群友发的任何图片或被 `@` 看的图！
2. 【优先级唤醒信标队列 (Trigger Queue + Waiting Handshake)】：
   - 采用内存优先级队列 `trigger_queue`（被 `@`/私聊高优插队，同群自动合并去重）+ `waiting.flag` 握手协议。
   - 多群触发会合并排队；唤醒队列仍在内存中，重启前需处理完积压。
3. 【统一本地会话调度】：
   - @/私聊即时进入优先队列，普通输入先积累上下文，再按价值与参与负担评分。
   - 同一公式判断相关续聊、群活跃时退让以及近期调用/发言/静默的衰减压力。
   - 本地评分不调用模型；旧巡检仅在关闭新调度时用于回退。
"""
from hakimi.core import bot_identity
from hakimi import paths

import asyncio
import html
import io
import json
import math
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.request
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from PIL import Image
from hakimi.transport.image_permissions import register_cached_image
from hakimi.models.fallback_supervisor import coordinator_for, fallback_loop
from hakimi.bridge.participation_policy import (exploration_context_allowed,
                                  rank_exploration_candidates, reserve_exploration,
                                  study_reply_allowed, _policy_and_context,
                                  _read_attempts, _within_budget)
import websockets
from hakimi.bridge.delivery_guard import DeliveryGuard
from hakimi.bridge.conversation_scheduler import ConversationScheduler, enabled as adaptive_enabled
from hakimi.core.runtime_io import read_json_cached, invalidate
from hakimi.core.context_window import select_context
from hakimi.transport.forward_messages import expand_forward, message_text
from hakimi.tools.bilibili_video import normalize_cards
from hakimi.features.codex_help import report_fault, project_root
from hakimi.tools.feeding import record_interaction
from hakimi.bridge.cold_topics import cold_store, MODE as COLD_MODE, settings as cold_settings

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = paths.base()
CONFIG_PATH = BASE_DIR / "config.json"
STATE_DIR = BASE_DIR / "state"
IMG_CACHE_DIR = STATE_DIR / "img_cache"
STATE_DIR.mkdir(exist_ok=True)
IMG_CACHE_DIR.mkdir(exist_ok=True)

INBOX_LOG = STATE_DIR / "chat_history.jsonl"
PENDING_TRIGGER = STATE_DIR / "pending_trigger.json"
WAITING_FLAG = STATE_DIR / "waiting.flag"
OUTBOX_FILE = STATE_DIR / "outbox.jsonl"
SIM_INPUT_FILE = STATE_DIR / "simulate_in.jsonl"
ONLINE_STATUS_FILE = STATE_DIR / "qq_online.json"

HTTP_API = "http://127.0.0.1:3000"
ACTIVE_WS_CLIENTS: dict = {}
PENDING_ACKS: dict = {}
INCOMING_EVENTS = asyncio.Queue(maxsize=2000)
RECENT_EVENT_IDS: OrderedDict = OrderedDict()
DELIVERY_STATE_FILE = STATE_DIR / "delivery_state.json"
STUDY_ATTENTION_SECONDS = 150
HISTORY_IO_LOCK = threading.RLock()


def load_config() -> dict:
    return read_json_cached(CONFIG_PATH)


def save_config(cfg: dict):
    temporary = CONFIG_PATH.with_suffix(".runtime.tmp")
    temporary.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CONFIG_PATH)
    invalidate(CONFIG_PATH)


def direct_attention(event, cfg):
    raw = str(event.get("raw_message", ""))
    self_id = str(event.get('self_id', cfg.get("bot_profile", {}).get("self_qq", bot_identity.SELF_QQ)))
    if event.get('message_type') == 'private':
        return True
    mentioned = f"[CQ:at,qq={self_id}]" in raw or "@哈基米" in raw
    if mentioned and event.get('user_id') is not None:
        from hakimi.social.social_behavior import should_reject_at
        session = 'group:' + str(event.get('group_id', ''))
        if should_reject_at(BASE_DIR, session, str(event['user_id']), config=cfg):
            return False
    return mentioned


def onebot_http_post(endpoint: str, payload: dict, timeout: float = 6.0) -> dict:
    try:
        req = urllib.request.Request(
            f"{HTTP_API}/{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def cache_qq_image_to_jpg(file_Param: str, url_param: str, tag_id: str) -> str | None:
    """将 QQ 图片通过 NapCat get_image 或 URL 下载并转换为标准 RGB JPG 存入 state/img_cache/"""
    tag_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(tag_id))[:160]
    out_path = IMG_CACHE_DIR / f"img_{tag_id}.jpg"
    if out_path.exists() and out_path.stat().st_size > 100:
        return out_path.as_posix()

    raw_bytes = None
    # 优先通过 NapCat get_image 获取本地原图路径
    if file_Param:
        resp = onebot_http_post("get_image", {"file": file_Param}, timeout=6.0)
        local_src = resp.get("data", {}).get("file")
        if local_src and Path(local_src).exists():
            try:
                raw_bytes = Path(local_src).read_bytes()
            except Exception:
                pass
        if not raw_bytes and resp.get("data", {}).get("url"):
            url_param = resp["data"]["url"]

    # 备选：直接从 URL 下载
    if not raw_bytes and url_param:
        clean_u = html.unescape(url_param)
        try:
            req = urllib.request.Request(clean_u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6.0) as r:
                raw_bytes = r.read()
        except Exception:
            pass

    if not raw_bytes:
        return None

    try:
        # 如果原图本身是 GIF 动图，同时保留原版 .gif 供 save_sticker 收藏原汁原味动图！
        if raw_bytes[:6] in (b"GIF87a", b"GIF89a"):
            gif_path = IMG_CACHE_DIR / f"img_{tag_id}.gif"
            gif_path.write_bytes(raw_bytes)

        img = Image.open(io.BytesIO(raw_bytes))
        # 提取第一帧转 RGB 存入 .jpg，确保 view_file 100% 兼容可读
        if getattr(img, "is_animated", False):
            img.seek(0)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        # 限制最大边长 1280 节省视觉 Token
        img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        img.save(out_path, "JPEG", quality=88)
        return out_path.as_posix()
    except Exception as e:
        print(f"   [图片缓存转换失败] {tag_id}: {e}")
        return None


def format_clean_message(raw_text: str, self_id: str, msg_id: str | int, msg_lookup: dict[str, dict],
                         image_paths: list[str] | None = None, session_key: str | None = None,
                         _forward_depth=0, _media_budget=None) -> str:
    """将含 CQ 码的原始消息转换为带本地图片缓存路径与引用内容的清晰文本"""
    text = re.sub(rf"\[CQ:at,qq={self_id}[^\]]*\]\s*", "@哈基米 ", normalize_cards(raw_text))
    if _media_budget is None:
        _media_budget = {"images": 2, "fetches": 2}

    # 1. 简化 QQ 小黄豆表情 [CQ:face,id=...,raw={...}]
    def _repl_face(m):
        raw_part = m.group(0)
        ft = re.search(r'faceText"\s*:\s*"([^"]+)"', html.unescape(raw_part))
        return f"[表情:{ft.group(1)}]" if ft else "[QQ表情]"

    text = re.sub(r"\[CQ:face,[^\]]+\]", _repl_face, text)

    # 2. 解析并缓存 [CQ:image,...]
    img_idx = 0

    def _repl_image(m):
        nonlocal img_idx
        if _forward_depth:
            if not _media_budget["images"]:
                return "[转发内图片，未加载像素]"
            _media_budget["images"] -= 1
        full_cq = m.group(0)
        img_idx += 1
        f_match = re.search(r"file=([^,\]]+)", full_cq)
        u_match = re.search(r"url=([^,\]]+)", full_cq)
        is_meme = "动画表情" in html.unescape(full_cq) or "sub_type=1" in full_cq
        file_val = f_match.group(1) if f_match else ""
        url_val = u_match.group(1) if u_match else ""
        local_jpg = cache_qq_image_to_jpg(file_val, url_val, f"{session_key or 'unscoped'}_{msg_id}_{img_idx}")
        kind_label = "表情包图片" if is_meme else "群友发图"
        if local_jpg:
            if session_key:
                register_cached_image(session_key, local_jpg)
                cached_gif = Path(local_jpg).with_suffix(".gif")
                if cached_gif.is_file():
                    register_cached_image(session_key, cached_gif)
            if image_paths is not None:
                image_paths.append(local_jpg)
                gif_path = Path(local_jpg).with_suffix(".gif")
                if gif_path.is_file():
                    image_paths.append(gif_path.as_posix())
            return f"[{kind_label}(可用view_file看图): {local_jpg}]"
        return f"[{kind_label}]"

    text = re.sub(r"\[CQ:image,[^\]]+\]", _repl_image, text)

    # 3. 解析 [CQ:reply,id=...] 引用内容（让哈基米知道群友在针对哪句话或哪张图说话！）
    def _repl_reply(m):
        rep_id = m.group(1)
        if rep_id in msg_lookup and session_key and msg_lookup[rep_id].get("session_key") == session_key:
            ref = msg_lookup[rep_id]
            if image_paths is not None:
                image_paths.extend(ref.get("image_paths", []))
            return f"[引用 {ref['nickname']} 的消息(id={rep_id}): \"{ref['text']}\"] "
        # 尝试通过 NapCat get_msg 查询被引用的历史消息
        resp = onebot_http_post("get_msg", {"message_id": int(rep_id)}, timeout=4.0)
        data = resp.get("data", {})
        same_session = False
        if isinstance(data, dict) and session_key:
            kind, target_id = session_key.split(":", 1)
            if kind == "group":
                same_session = str(data.get("group_id", "")) == target_id
            elif kind == "private" and data.get("message_type") == "private":
                peer_id = data.get("user_id") or data.get("sender", {}).get("user_id")
                same_session = str(peer_id) == target_id
        if data and same_session:
            ref_sender = data.get("sender", {})
            ref_nick = ref_sender.get("card") or ref_sender.get("nickname") or str(data.get("user_id", ""))
            ref_raw = data.get("raw_message", "")
            # 递归解析被引用消息里的图片（不递归引用避免死循环）
            ref_clean = format_clean_message(
                re.sub(r"\[CQ:reply,id=-?\d+\]", "", ref_raw),
                self_id,
                rep_id,
                {},
                image_paths,
                session_key,
            )
            return f"[引用 {ref_nick} 的消息(id={rep_id}): \"{ref_clean}\"] "
        return f"[引用消息(id={rep_id})] "

    text = re.sub(r"\[CQ:reply,id=(-?\d+)\]\s*", _repl_reply, text)
    if "[CQ:forward," in text or "[CQ:json," in text or "[CQ:xml," in text:
        def fetch_forward(forward_id):
            if _forward_depth >= 2 or not _media_budget["fetches"]:
                return {}
            _media_budget["fetches"] -= 1
            return onebot_http_post("get_forward_msg", {"message_id": forward_id, "id": forward_id}, timeout=4.0)
        def render_content(content):
            raw = re.sub(r"\[CQ:reply,id=-?\d+\]", "[转发内引用]", message_text(content))
            return format_clean_message(raw, self_id, f"{msg_id}_f{_media_budget['images']}_{_media_budget['fetches']}",
                                        {}, image_paths, session_key, _forward_depth+1, _media_budget)
        text = expand_forward(text, fetch_forward, render_content)
    return text.strip()


class DynamicHakimiEngine:
    def __init__(self):
        self.context_windows: dict[str, deque] = {}
        self.msg_lookup: dict[str, dict] = {}
        self.recent_msg_timestamps: dict[str, deque] = {}
        self.unread_counts: dict[str, int] = {}
        self.window_interest_score: dict[str, float] = {}
        self.window_hit_reasons: dict[str, list[str]] = {}

        # 动态阈值与状态追踪
        self.group_score_threshold: dict[str, float] = {}
        self.cooldown_until: dict[str, float] = {}
        self.last_replied_at: dict[str, float] = {}
        self.active_streak_count: dict[str, int] = {}
        self.last_msg_is_self: dict[str, bool] = {}

        self.last_any_wake_time: float = time.time()
        self.next_proactive_threshold: float = random.uniform(2400, 4800)

        self.last_wake_info: dict | None = None
        self.pending_feedback: dict[str, dict] = {}
        self.recovered_unread_pending = False
        self.fast_peek_event = asyncio.Event()
        self.study_schedule_event = asyncio.Event()
        self.study_attention_deadlines: dict[str, float] = {}
        self.conversation_scheduler = ConversationScheduler()
        self.reply_review_event = asyncio.Event()
        self.conversation_scheduler.on_window_closed = self.close_reply_review
        # 待消费唤醒队列（避免多群并发唤醒丢失）
        self.trigger_queue: deque[dict] = deque()

    def get_group_weight_info(self, group_id: str, cfg: dict) -> dict:
        gw = cfg.get("group_weights", {}).get(group_id, {})
        return {
            "name": gw.get("name", f"群{group_id}"),
            "weight": float(gw.get("weight", 0.35)),
            "min_score": float(gw.get("min_score", self.group_score_threshold.get(f"group:{group_id}", 0.20))),
            "theme": gw.get("theme", "日常闲聊、二次元、学习或游戏吐槽"),
        }

    def update_group_dynamic_params(self, group_id: str, weight_delta: float, score_thresh_delta: float, reason: str):
        try:
            cfg = load_config()
            gw_map = cfg.setdefault("group_weights", {})
            info = gw_map.setdefault(group_id, {
                "name": f"群{group_id}",
                "weight": 0.4,
                "min_score": 0.20,
                "theme": "日常群聊话题"
            })
            old_w = float(info.get("weight", 0.4))
            old_s = float(info.get("min_score", 0.20))
            ceiling = float(cfg.get("lurker_schedule", {}).get("adaptive_weight_ceiling", 1.5))
            new_w = round(max(0.10, min(ceiling, old_w + weight_delta)), 2)
            new_s = round(max(0.10, min(1.20, old_s + score_thresh_delta)), 2)
            info["weight"] = new_w
            info["min_score"] = new_s
            self.group_score_threshold[f"group:{group_id}"] = new_s
            save_config(cfg)
            print(f"   [动态参数自整定] 群 {group_id}({info.get('name')}): weight {old_w:.2f}->{new_w:.2f}, 唤醒门槛 {old_s:.2f}->{new_s:.2f} ({reason})")
        except Exception as e:
            print(f"动态调整参数出错: {e}")

    def record_and_score(self, event: dict, cfg: dict, clean_text: str | None = None,
                         image_paths: list[str] | None = None, memory_nodes=()) -> tuple[bool, list[str], str]:
        msg_type = event.get("message_type", "group")
        user_id = str(event.get("user_id", ""))
        self_id = str(event.get("self_id", cfg["bot_profile"].get("self_qq", bot_identity.SELF_QQ)))
        group_id = str(event.get("group_id", "")) if msg_type == "group" else "private"
        session_key = f"{msg_type}:{group_id if msg_type == 'group' else user_id}"

        active_groups = [str(g) for g in cfg["bot_profile"].get("active_groups", [])]
        ignored_groups = [str(g) for g in cfg["bot_profile"].get("ignored_groups", [])]
        if msg_type == "group":
            if active_groups and group_id not in active_groups:
                return False, ["非白名单群"], ""
            if group_id in ignored_groups:
                return False, ["黑名单忽略群"], ""
        elif not cfg["bot_profile"].get("allow_private_chat", True):
            return False, ["私聊已关闭"], ""

        raw_text = event.get("raw_message", "").strip()
        topic_cfg = cfg["topic_bias"]
        now = time.time()
        nickname = event.get("sender", {}).get("card") or event.get("sender", {}).get("nickname", f"QQ_{user_id}")
        msg_id = str(event.get("message_id", int(now * 1000)))
        if image_paths is None:
            image_paths = []
        if clean_text is None:
            clean_text = format_clean_message(raw_text, self_id, msg_id, self.msg_lookup, image_paths, session_key)

        reply_ref=re.search(r"\[CQ:reply,id=(-?\d+)\]",raw_text)
        reply_id=reply_ref[1] if reply_ref else ''
        msg_item = {
            "self_id":self_id,
            "direct_targets":sorted(set(re.findall(r"\[CQ:at,qq=(\d+)\]",raw_text))),
            "reply_to":reply_id,
            "reply_user_id":str(self.msg_lookup.get(reply_id,{}).get('user_id','')),
            "time": time.strftime("%H:%M:%S"),
            "received_at": RECENT_EVENT_IDS.get((session_key, str(event.get("message_id"))), now),
            "message_id": event.get("message_id", msg_id),
            "session_key": session_key,
            "user_id": user_id,
            "nickname": nickname,
            "text": clean_text,
            "image_paths": list(dict.fromkeys(image_paths)),
        }
        if event.get('qq_prior_messages'):
            # The attached excerpt travels with the mention. Only the actual message is scored and indexed.
            msg_item['qq_prior_messages'] = event['qq_prior_messages']
        self.msg_lookup[msg_id] = msg_item
        if len(self.msg_lookup) > 500:
            oldest_k = next(iter(self.msg_lookup))
            del self.msg_lookup[oldest_k]

        if session_key not in self.context_windows:
            self.context_windows[session_key] = deque(maxlen=80)
        self.context_windows[session_key].append(msg_item)
        window_state = self.conversation_scheduler.group(session_key)
        if window_state.window_collecting and msg_item["received_at"] > window_state.context_high_water:
            window_state.window_messages.append(dict(msg_item))
        self.unread_counts[session_key] = self.unread_counts.get(session_key, 0) + 1
        self.last_msg_is_self[session_key] = False

        from hakimi.bridge.bot_registry import cooling
        if cooling(BASE_DIR,user_id,now):return False,['机器人接话冷却，仅记录上下文'],clean_text
        ts_q = self.recent_msg_timestamps.setdefault(session_key, deque(maxlen=20))
        ts_q.append(now)
        msgs_in_60s = sum(1 for t in ts_q if now - t <= 60)

        if adaptive_enabled(cfg):
            mentioned = direct_attention(event, cfg)
            reply_match = re.search(r"\[CQ:reply,id=(-?\d+)\]", raw_text)
            received_at = RECENT_EVENT_IDS.get((session_key, str(event.get("message_id"))), now)
            at_targets = re.findall(r"\[CQ:at,qq=([0-9]+)\]", raw_text)
            self.conversation_scheduler.observe(session_key, event.get('_voice_semantic') or clean_text, user_id, msg_id, received_at,
                mentioned=mentioned, image=bool(image_paths), reply_to=reply_match.group(1) if reply_match else None,
                addressed_other=bool(at_targets) and not mentioned,memory_nodes=memory_nodes)
            decision = self.conversation_scheduler.decide(session_key, cfg, now)
            self.conversation_scheduler.audit_decision(session_key, decision, now)
            self.window_interest_score[session_key] = decision.score
            self.study_schedule_event.set()
            instant = decision.mode == "mention"
            return instant, ["明确提问立即提交" if instant else "本地积累上下文，统一参与评分"], clean_text

        if msg_type == "private":
            return True, ["私聊即时唤醒"], clean_text
        if session_key in self.pending_feedback and msg_type == "group":
            fb = self.pending_feedback[session_key]
            fb["msgs_since"] += 1
            is_at = direct_attention(event, cfg)
            if is_at:
                self.update_group_dynamic_params(group_id, +0.03, -0.01, "发言后被明确提及")
                del self.pending_feedback[session_key]

        gw_info = self.get_group_weight_info(group_id, cfg) if msg_type == "group" else {"weight": 1.0, "min_score": 0.15}
        g_weight = gw_info["weight"]
        score_delta = 0.10 * g_weight
        reasons = []

        u_info = cfg["user_bias"].get("users", {}).get(user_id, {})
        u_bonus = u_info.get("prob_bonus", 0.0)
        if u_bonus != 0:
            score_delta += u_bonus * g_weight
            reasons.append(f"熟人[{nickname}]({u_bonus:+.2f})")

        for kw, kw_bonus in topic_cfg.get("favorite_keywords", {}).items():
            if kw.lower() in clean_text.lower():
                score_delta += kw_bonus * g_weight
                reasons.append(f"话题[{kw}](+{kw_bonus:.2f}×w{g_weight:.1f})")

        since_last_reply = now - self.last_replied_at.get(session_key, 0)
        can_peek = msg_type != "group" or group_id in {str(g) for g in cfg["bot_profile"].get("random_peek_groups", [])}
        streak = self.active_streak_count.get(session_key, 0)
        already_pending = (
            (self.last_wake_info and self.last_wake_info.get("session_key") == session_key)
            or any(item["session_key"] == session_key for item in self.trigger_queue)
        )
        if can_peek and 0 < since_last_reply <= 90 and streak < 3 and now >= self.cooldown_until.get(session_key, 0) and not already_pending:
            score_delta += 0.45
            reasons.append(f"热聊余温期({int(since_last_reply)}s内接话, 第{streak+1}/3回合)")
            self.fast_peek_event.set()

        if can_peek and msgs_in_60s >= 6 and now >= self.cooldown_until.get(session_key, 0) and not already_pending:
            score_delta += 0.25
            reasons.append(f"群聊流速爆发(60s内{msgs_in_60s}条)")
            self.fast_peek_event.set()

        from hakimi.social.social_behavior import behavior_for
        social = behavior_for(BASE_DIR, user_id, session_key, now=now, config=cfg)
        score_delta *= social['initiative_multiplier']
        self.window_interest_score[session_key] = self.window_interest_score.get(session_key, 0.0) + score_delta
        if reasons:
            self.window_hit_reasons.setdefault(session_key, []).extend(reasons)

        lurk_cfg = cfg.get("lurker_schedule", {})
        is_at = direct_attention(event, cfg)

        # 防恶意刷屏骚扰：对持续无意义刷屏 @ 的群友不触发即时唤醒（owner.json: local.no_instant_wake_ids）
        from hakimi.core.owner import local_terms
        if user_id in local_terms("no_instant_wake_ids"):
            return False, reasons, clean_text

        if lurk_cfg.get("instant_wake_on_at_or_private", True):
            if is_at and random.random() < lurk_cfg.get("at_me_prob", 0.95):
                return True, ["群内被@即时唤醒"] + reasons, clean_text

        return False, reasons, clean_text

    def check_skip_and_expired_feedback(self):
        if adaptive_enabled(load_config()):
            return  # Only actual coordinator outcomes count as silence, not a timer.
        now = time.time()
        if self.last_wake_info and not PENDING_TRIGGER.exists():
            if now - self.last_wake_info["time"] > 75:
                sk = self.last_wake_info["session_key"]
                mode = self.last_wake_info["mode"]
                if sk.startswith("group:") and mode == "随机时间窗窥屏":
                    gid = sk.split(":", 1)[1]
                    self.update_group_dynamic_params(gid, -0.04, +0.10, "Flash窥屏后判定话题无聊跳过(Skip)")
                self.last_wake_info = None

        for sk, fb in list(self.pending_feedback.items()):
            if now - fb["time"] > 1800:
                if fb["msgs_since"] == 0 and fb.get("is_proactive") and sk.startswith("group:"):
                    gid = sk.split(":", 1)[1]
                    self.update_group_dynamic_params(gid, -0.08, +0.05, "主动抛话题30分钟内无人接话")
                del self.pending_feedback[sk]

    def pick_session_for_random_peek(self, cfg: dict, fast_only: bool = False) -> tuple[str | None, float, list[str]]:
        now = time.time()
        lurk_cfg = cfg.get("lurker_schedule", {})
        default_min_msgs = lurk_cfg.get("min_new_messages_to_peek", 2)

        candidates = []
        for sk, unread in list(self.unread_counts.items()):
            if not session_is_allowed(sk, cfg):
                continue
            gid = sk.split(":", 1)[1] if sk.startswith("group:") else ""
            standard_groups = {str(g) for g in cfg.get("bot_profile", {}).get("random_peek_groups", [])}
            group_weight = float(cfg.get("group_weights", {}).get(gid, {}).get("weight", 0.35))
            low_groups = {str(g) for g in cfg.get("bot_profile", {}).get("low_frequency_groups", [])}
            explore = bool(gid and (gid not in standard_groups or gid in low_groups or group_weight <= 0.8))
            timestamps = self.recent_msg_timestamps.get(sk, deque())
            last_human_at = timestamps[-1] if timestamps else 0
            if explore and fast_only:
                continue
            if now < self.cooldown_until.get(sk, 0):
                continue
            in_warm_window = 0 < (now - self.last_replied_at.get(sk, 0)) <= 90 and self.active_streak_count.get(sk, 0) < 3
            ts_q = self.recent_msg_timestamps.get(sk, deque())
            msgs_in_60s = sum(1 for t in ts_q if now - t <= 60)
            is_burst = msgs_in_60s >= 6
            if fast_only and not (in_warm_window or is_burst):
                continue

            req_msgs = 1 if in_warm_window or explore else default_min_msgs
            if unread < req_msgs:
                continue

            score = self.window_interest_score.get(sk, 0.1)
            gid = sk.split(":", 1)[1] if sk.startswith("group:") else ""
            min_score = self.get_group_weight_info(gid, cfg)["min_score"] if gid else 0.15
            if score < min_score and not in_warm_window and not explore:
                continue

            candidates.append((sk, min(score, 0.5) if explore else score, unread, in_warm_window, explore))

        if not candidates:
            if not fast_only:
                self.clear_study_attention()
            return None, 0.0, []

        # An ordinary due slot can replace one hot-group peek with a fair
        # exploration opportunity. This neither adds a model turn nor forces a reply.
        by_session = {c[0]: c for c in candidates}
        fair_sessions = rank_exploration_candidates(
            [(c[0], self.recent_msg_timestamps.get(c[0], [0])[-1]
              if self.recent_msg_timestamps.get(c[0]) else 0) for c in candidates if c[4]],
            now, cfg, BASE_DIR, last_replied_at=self.last_replied_at,
        )
        if not fast_only:
            # Full history checks can reject a group the cheap receive-side
            # prefilter admitted. Consume that local check instead of letting
            # its past deadline repeatedly accelerate unrelated hot groups.
            for pending in list(self.study_attention_deadlines):
                if pending not in fair_sessions:
                    self.clear_study_attention(pending)
        if fair_sessions:
            chosen = by_session[fair_sessions[0]]
        else:
            candidates = [c for c in candidates if not c[4]]
            if not candidates:
                return None, 0.0, []
            total_score = sum(max(0.05, c[1]) for c in candidates)
            r = random.uniform(0, total_score)
            upto = 0.0
            chosen = candidates[0]
            for c in candidates:
                w = max(0.05, c[1])
                if upto + w >= r:
                    chosen = c
                    break
                upto += w

        sk, score, unread, in_warm, explore = chosen
        tag = "低频尝试参与" if explore else ("余温顺势接话" if in_warm else "随机时间窗抽中")
        reasons = [f"{tag}(未读{unread}条, 兴趣分{score:.2f})"] + self.window_hit_reasons.get(sk, [])[-5:]
        return sk, score, reasons

    def pick_group_for_proactive_topic(self, cfg: dict) -> tuple[str | None, dict, list[str]]:
        now = time.time()
        pro_cfg = cfg.get("lurker_schedule", {}).get("proactive_topic", {})
        if not pro_cfg.get("enable", True):
            return None, {}, []

        idle_sec = now - self.last_any_wake_time
        if idle_sec < self.next_proactive_threshold:
            return None, {}, []

        min_quiet = pro_cfg.get("min_group_quiet_seconds", 1800)
        gw_map = cfg.get("group_weights", {})
        candidates = []
        for gid, info in gw_map.items():
            if str(gid) not in {str(g) for g in cfg["bot_profile"].get("random_peek_groups", [])}:
                continue
            sk = f"group:{gid}"
            w = float(info.get("weight", 0.3))
            if w <= 0.1:
                continue
            if now - self.last_replied_at.get(sk, 0) < min_quiet:
                continue
            if pro_cfg.get("require_last_msg_not_self", True) and self.last_msg_is_self.get(sk, False):
                continue
            candidates.append((sk, gid, w, info))

        if not candidates:
            return None, {}, []

        total_w = sum(c[2] for c in candidates)
        r = random.uniform(0, total_w)
        upto = 0.0
        chosen = candidates[0]
        for c in candidates:
            if upto + c[2] >= r:
                chosen = c
                break
            upto += c[2]

        sk, gid, w, info = chosen
        reasons = [
            f"全服已静默 {int(idle_sec/60)} 分钟，触发【冷场主动抛话题】",
            f"按动态群权重抽中: {info.get('name')} (weight={w:.2f})",
            f"该群主题印象: {info.get('theme')}"
        ]
        return sk, info, reasons

    def clear_unread(self, session_key: str, through=None):
        self.unread_counts[session_key] = 0
        self.window_interest_score[session_key] = 0.0
        self.window_hit_reasons[session_key] = []
        self.clear_study_attention(session_key)
        self.conversation_scheduler.consume(session_key, through=through)

    def _study_attention_eligible(self, session_key, cfg, now, base=BASE_DIR):
        """Cheap scheduling prefilter; full reply-history checks remain at selection."""
        supplied = cfg.get("participation_policy", {})
        if not isinstance(supplied, dict):
            return False
        targets = supplied.get("study_attention_groups", [])
        if (not isinstance(targets, (list, tuple)) or len(targets) > 10000
                or session_key not in {f"group:{group}" for group in targets}
                or not cfg.get("lurker_schedule", {}).get("enable_random_window_peek", True)
                or self.unread_counts.get(session_key, 0) <= 0
                or now < self.cooldown_until.get(session_key, 0)
                or any(item["session_key"] == session_key for item in self.trigger_queue)):
            return False
        timestamps = self.recent_msg_timestamps.get(session_key, ())
        policy = _policy_and_context(session_key, timestamps[-1] if timestamps else 0, now, cfg)
        if policy is None or not policy["is_study"]:
            return False
        last_reply = self.last_replied_at.get(session_key, 0)
        if last_reply and now - last_reply < policy["study_min_reply_interval_seconds"]:
            return False
        path = Path(base) / "state" / "participation_state.json"
        try:
            # Reuse the policy's bounded checkpoint parsing and quota rules;
            # this path does not read outbox bodies or reserve an attempt.
            return not path.with_suffix(".lock").exists() and _within_budget(
                session_key, now, policy, _read_attempts(path, now))
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            return False

    def arm_study_attention(self, session_key, cfg, received_at=None, *, now=None, base=BASE_DIR, immediate=False):
        if session_key in self.study_attention_deadlines:
            return False  # A later message must never postpone the first receipt.
        now = time.time() if now is None else now
        received_at = now if received_at is None else received_at
        if (type(received_at) not in (int, float) or not math.isfinite(received_at)
                or not 0 <= now - received_at <= 600
                or not self._study_attention_eligible(session_key, cfg, now, base)):
            return False
        remaining = 0 if immediate else max(0, STUDY_ATTENTION_SECONDS - (now - received_at))
        self.study_attention_deadlines[session_key] = time.monotonic() + remaining
        self.study_schedule_event.set()
        return True

    def clear_study_attention(self, session_key=None):
        if session_key is None:
            changed = bool(self.study_attention_deadlines)
            self.study_attention_deadlines.clear()
        else:
            changed = self.study_attention_deadlines.pop(session_key, None) is not None
        if changed:
            self.study_schedule_event.set()

    def next_study_attention_deadline(self, cfg, *, now=None, base=BASE_DIR):
        now = time.time() if now is None else now
        for session in list(self.study_attention_deadlines):
            if not self._study_attention_eligible(session, cfg, now, base):
                self.study_attention_deadlines.pop(session, None)
        return min(self.study_attention_deadlines.values(), default=None)

    def restore_recent_study_unread(self, cfg: dict, base=BASE_DIR, now=None):
        """Restore only validated study messages; never replay receive-side effects."""
        from hakimi.bridge.study_recovery import recover_study_messages
        now = time.time() if now is None else now
        recovery_cfg = cfg
        if adaptive_enabled(cfg):
            configured = set(cfg.get("group_weights", {}))
            for key in ("active_groups", "low_frequency_groups", "random_peek_groups"):
                configured.update(str(v) for v in cfg.get("bot_profile", {}).get(key, []))
            recovery_cfg = {**cfg, "participation_policy": {**cfg.get("participation_policy", {}),
                                                          "study_groups": sorted(configured)}}
        recovered = recover_study_messages(base, recovery_cfg, now, reply_watermarks=not adaptive_enabled(cfg))
        if not recovered:
            return 0
        count = 0
        for row in recovered["messages"]:
            session = row["session_key"]
            message_id = str(row["message_id"])
            event_key = (session, message_id)
            if event_key in RECENT_EVENT_IDS:
                continue
            if any(str(item.get("message_id")) == message_id
                   for item in self.context_windows.get(session, ())):
                continue
            message = {key: row[key] for key in
                       ("time", "message_id", "session_key", "user_id", "nickname", "text")}
            message["image_paths"] = []
            self.context_windows.setdefault(session, deque(maxlen=80)).append(message)
            self.msg_lookup[message_id] = message
            self.unread_counts[session] = self.unread_counts.get(session, 0) + 1
            self.recent_msg_timestamps.setdefault(session, deque(maxlen=20)).append(row["received_at"])
            self.window_interest_score[session] = min(0.5, self.window_interest_score.get(session, 0) + 0.1)
            self.window_hit_reasons[session] = ["重启恢复新鲜学习群未读"]
            self.last_msg_is_self[session] = False
            RECENT_EVENT_IDS[event_key] = row["received_at"]
            count += 1
        while len(self.msg_lookup) > 500:
            self.msg_lookup.pop(next(iter(self.msg_lookup)))
        while len(RECENT_EVENT_IDS) > 500:
            RECENT_EVENT_IDS.popitem(last=False)
        for session, at in recovered["last_replied_at"].items():
            self.last_replied_at[session] = max(self.last_replied_at.get(session, 0), at)
            if adaptive_enabled(cfg):
                state = self.conversation_scheduler.group(session)
                if not state.last_reply:
                    if state.at <= at:
                        self.conversation_scheduler.sent_reply(session, "", at)
                    else:
                        state.last_reply = at
        if adaptive_enabled(cfg):
            for row in recovered["messages"]:
                session = row["session_key"]
                if row["received_at"] > self.conversation_scheduler.group(session).last_offer:
                    mentioned = direct_attention({"raw_message": row["text"], "message_type": "group", "user_id": row["user_id"], "group_id": session.split(":",1)[1]}, cfg)
                    targets = re.findall(r"\[CQ:at,qq=([0-9]+)\]", row["text"])
                    self.conversation_scheduler.observe(session, row["text"], row["user_id"], row["message_id"], row["received_at"],
                        mentioned=mentioned, addressed_other=bool(targets) and not mentioned)
            self.study_schedule_event.set()
            self.recovered_unread_pending = self.recovered_unread_pending or count > 0
            return count
        for session in {row["session_key"] for row in recovered["messages"]}:
            self.arm_study_attention(session, cfg, now=now, base=base, immediate=True)
        self.recovered_unread_pending = self.recovered_unread_pending or count > 0
        return count

    def restore_listening_unread(self, now=None):
        """Recover only active listening windows from the existing local inbox."""
        now = time.time() if now is None else now
        scheduler = self.conversation_scheduler
        active = {key for key, state in scheduler.groups.items() if state.listen_round}
        if not active:return
        for row in (item for session in active for item in conversation_window_rows(
                session, scheduler.group(session).listen_started, now)):
            session, at = row.get("session_key"), row.get("received_at")
            if (session not in active or row.get("user_id") == "SELF" or type(at) not in (int, float)
                    or not 0 <= now-at <= 600):continue
            state = scheduler.group(session)
            if at <= state.listen_started:continue
            state.window_messages.append(dict(row))
            window = self.context_windows.setdefault(session, deque(maxlen=80))
            if str(row.get("message_id")) not in {str(item.get("message_id")) for item in window}:
                window.append(row)
                self.unread_counts[session] = self.unread_counts.get(session, 0)+1
            text = str(row.get("text", ""))
            scheduler.observe(session, text, str(row.get("user_id", "")), row.get("message_id", ""), at,
                mentioned=direct_attention({'raw_message':text,'message_type':'group',
                    'user_id':row.get('user_id'),'group_id':session.split(':',1)[1]}, load_config()),
                image=bool(row.get("image_paths")))
        self.study_schedule_event.set()

    def close_reply_review(self, session, delivering=False):
        try:
            from hakimi.memory.post_reply_review import close
            close(BASE_DIR,session,time.time(),delivering=delivering,
                  humans=list(self.context_windows.get(session, ())))
            self.reply_review_event.set()
        except Exception:
            report_fault(BASE_DIR,'post_reply_review',code='REVIEW_CLOSE_FAILED')

    def record_reply_review(self, session, receipt_id, text, message_id, trigger, now, targets):
        try:
            from hakimi.memory.post_reply_review import receipt
            receipt(BASE_DIR,session,receipt_id,text,message_id,trigger,now,
                    humans=list(self.context_windows.get(session, ())),target_ids=targets)
        except Exception:
            report_fault(BASE_DIR,'post_reply_review',code='REVIEW_RECEIPT_FAILED')

    def finish_reply_review(self, session, now,trigger_id=''):
        from hakimi.voice.store import scoped_turn
        if scoped_turn(BASE_DIR,session,trigger_id):return
        try:
            from hakimi.memory.post_reply_review import delivery_finished
            state=self.conversation_scheduler.groups.get(session)
            delivery_finished(BASE_DIR,session,now,listening=bool(state and state.listen_round),
                              humans=list(self.context_windows.get(session, ())))
            self.reply_review_event.set()
        except Exception:
            report_fault(BASE_DIR,'post_reply_review',code='REVIEW_DELIVERY_FAILED')

    def mark_replied(self, session_key: str, cfg: dict, is_proactive: bool = False, *, reply_text="", message_id=None, trigger_id=""):
        now = time.time()
        source=self.conversation_scheduler.offer_sources.get(trigger_id,{})
        from hakimi.voice.store import scoped_turn
        if adaptive_enabled(cfg) and reply_text and trigger_id and source.get('session')==session_key and not scoped_turn(BASE_DIR,session_key,trigger_id):
            try:
                from hakimi.memory.memory_store import store
                question=' '.join(self.msg_lookup[mid]['text'] for mid in source.get('message_ids',[])
                                  if mid in self.msg_lookup and self.msg_lookup[mid].get('session_key')==session_key)
                nodes=store(str(BASE_DIR)).dialogue_signature(session_key,trigger_id,question,reply_text,now)
                source['memory_nodes']=list(dict.fromkeys(nodes+source.get('memory_nodes',[])))[:6]
            except (OSError,sqlite3.Error) as exc:
                report_fault(BASE_DIR,'topic_memory',exc)
        if is_proactive and cold_settings(cfg).get('enable',False):
            try:
                cold_store(str(BASE_DIR)).sent(session_key,trigger_id,reply_text,message_id,cfg,now)
            except (OSError,sqlite3.Error) as exc:
                report_fault(BASE_DIR,'cold_topic_memory',exc)
        adaptive = adaptive_enabled(cfg)
        if adaptive:
            self.conversation_scheduler.sent_reply(session_key, reply_text, now, message_id, trigger_id, cfg=cfg)
        if adaptive or session_key.startswith('private:'):
            self.last_replied_at[session_key] = now
            self.last_any_wake_time = now
            self.last_msg_is_self[session_key] = True
            self.last_wake_info = None
            # Keep human messages that arrived while the model was generating.
            self.study_schedule_event.set()
            return
        if now - self.last_replied_at.get(session_key, 0) <= 120:
            self.active_streak_count[session_key] = self.active_streak_count.get(session_key, 0) + 1
        else:
            self.active_streak_count[session_key] = 1

        streak = self.active_streak_count[session_key]
        base_cd = cfg.get("lurker_schedule", {}).get("cooldown_per_group_seconds", 300)
        if streak >= 3:
            cd = base_cd + random.randint(0, 240)
            self.cooldown_until[session_key] = now + cd
            self.active_streak_count[session_key] = 0
            print(f"   [动态冷却] {session_key} 已连续互动 {streak} 回合，进入 {cd}s 潜水冷却期~")
        else:
            self.cooldown_until[session_key] = now + 12

        self.last_replied_at[session_key] = now
        self.last_msg_is_self[session_key] = True
        self.last_any_wake_time = now
        self.last_wake_info = None

        pro_cfg = cfg.get("lurker_schedule", {}).get("proactive_topic", {})
        self.next_proactive_threshold = random.uniform(
            pro_cfg.get("min_global_idle_seconds", 2400),
            pro_cfg.get("max_global_idle_seconds", 4800),
        )
        self.pending_feedback[session_key] = {
            "time": now,
            "msgs_since": 0,
            "is_proactive": is_proactive,
        }
        self.clear_unread(session_key)


engine = DynamicHakimiEngine()
LAST_WAKE_WAS_PROACTIVE = False


def enqueue_wake_beacon(session_key: str, mode: str, score: float, reasons: list[str], cfg: dict, group_theme_info: dict | None = None, continuation_selection=None):
    """将唤醒请求加入优先级队列（被@即时唤醒优先插队，同群去重合并）"""
    is_instant = "即时唤醒" in mode
    direct = []
    if is_instant:
        rows = engine.context_windows.get(session_key, ())
        mentioned = {row[2] for row in engine.conversation_scheduler.group(session_key).pending if row[5]} if adaptive_enabled(cfg) else set()
        direct = [dict(row) for row in rows if str(row.get('message_id')) in mentioned][-3:]
        if not direct and rows and rows[-1].get("user_id") != "SELF":
            direct = [dict(rows[-1])]
    if is_instant:
        engine.clear_study_attention(session_key)
    # 检查队列中是否已有同一 session_key 的待处理项
    for item in list(engine.trigger_queue):
        if item["session_key"] == session_key:
            if is_instant and "即时唤醒" not in item["mode"]:
                item["mode"] = mode
                engine.trigger_queue.remove(item)
                engine.trigger_queue.appendleft(item)
            item["score"] = max(item["score"], score)
            item["reasons"] = list(dict.fromkeys(item["reasons"] + reasons))[-6:]
            if direct:
                anchors = item.get("direct_messages", []) + direct
                by_id = {str(row.get("message_id")): row for row in anchors}
                item["direct_messages"] = list(by_id.values())[-3:]
            print(f"   [唤醒队列合并] {session_key} ({mode}) 已合并至待处理队列 (当前队列长度: {len(engine.trigger_queue)})")
            _prewarm_mention_context(item, cfg)
            flush_trigger_queue_if_waiting()
            return

    entry = {
        "session_key": session_key,
        "mode": mode,
        "score": score,
        "reasons": reasons,
        "group_theme_info": group_theme_info,
    }
    if continuation_selection is not None:
        entry["continuation_selection"] = continuation_selection
    if direct:
        entry["direct_messages"] = direct
    _prewarm_mention_context(entry, cfg)
    if is_instant:
        engine.trigger_queue.appendleft(entry)
    else:
        engine.trigger_queue.append(entry)
    from hakimi.bridge.message_lifecycle import entry as audit_entry
    audit_entry(BASE_DIR,entry,'queued_local')
    print(f"   ★ [{mode}] 已入队 -> {session_key} ({' | '.join(reasons)}) [队列长度: {len(engine.trigger_queue)}]")
    flush_trigger_queue_if_waiting()



def _mention_inputs(entry, cfg, now):
    from hakimi.bridge.interaction_policy import ignored, ignored_text
    session_key = entry["session_key"]
    rows = [row for row in engine.context_windows.get(session_key, [])
            if not ignored(row.get('user_id'), BASE_DIR, cfg, session=session_key)
            and not ignored_text(row.get('text', ''), cfg)]
    anchors = [row for row in entry.get("direct_messages", [])[-3:]
               if row.get("session_key") == session_key
               and not ignored(row.get('user_id'), BASE_DIR, cfg, session=session_key)
               and not ignored_text(row.get('text', ''), cfg)
               and type(row.get("received_at")) in (int, float)
               and 0 <= now-row["received_at"] <= 600]
    if anchors:
        recent_ids = {str(row.get("message_id")) for row in rows}
        missing = [row for row in anchors if str(row.get("message_id")) not in recent_ids]
        if missing:
            rows = missing + rows[-(80-len(missing)):]
            rows.sort(key=lambda row: row.get("received_at", now))
    return rows, anchors


def _prewarm_mention_context(entry, cfg):
    """Overlap context review with the short mention collection window."""
    session_key = entry["session_key"]
    if not session_key.startswith('group:') or not entry["mode"].startswith('即时唤醒'):
        return
    if entry.get("mention_review") is not None:
        return
    rows, anchors = _mention_inputs(entry, cfg, time.time())
    if anchors:
        from hakimi.bridge.conversation_judge import mention_context
        state = engine.conversation_scheduler.group(session_key)
        mention_context(entry, rows, anchors, BASE_DIR,
                        previous_reply=state.listen_reply, own_message_ids=list(state.own_ids),
                        on_ready=engine.study_schedule_event.set)


def delivery_in_progress():
    """Keep the current context reservation until its outbox is consumed."""
    try:
        state = json.loads(DELIVERY_STATE_FILE.read_text(encoding="utf-8")) if DELIVERY_STATE_FILE.exists() else {}
        if state.get("pending"):
            return True
        cursor = state.get("cursor", 0)
        return OUTBOX_FILE.exists() and OUTBOX_FILE.stat().st_size > cursor
    except (OSError, ValueError, TypeError):
        return True


def flush_trigger_queue_if_waiting():
    """Publish one durable turn; primary gets first chance, dsh can recover it."""
    global LAST_WAKE_WAS_PROACTIVE
    if not engine.trigger_queue:
        return
    cfg = load_config()
    if not WAITING_FLAG.exists() and not cfg.get("fallback", {}).get("enabled", False):
        return
    coordinator = coordinator_for(BASE_DIR, cfg)
    entry = engine.trigger_queue[0]
    from hakimi.bridge.message_lifecycle import entry as audit_entry
    session_key = entry["session_key"]
    mode = entry["mode"]
    reply_lane = engine.conversation_scheduler.group(session_key).reply_lane
    probe = {'session_key': session_key, 'wake_mode': mode, 'conversation_lane': reply_lane}
    if coordinator.available_lane(probe) is None or (delivery_in_progress() and not mode.startswith('即时唤醒')):
        return
    score = entry["score"]
    reasons = entry["reasons"]
    group_theme_info = entry["group_theme_info"]

    now = time.time()
    if not session_is_allowed(session_key, cfg):
        audit_entry(BASE_DIR,entry,'cancelled',reason='session_not_allowed')
        engine.trigger_queue.popleft()
        return
    if mode.startswith("即时唤醒") and adaptive_enabled(cfg):
        decision = engine.conversation_scheduler.decide(session_key, cfg, now)
        if decision.mode == "mention" and not decision.ready:
            return
    from hakimi.transport.group_capabilities import can_autonomous
    if not mode.startswith("即时唤醒") and (not can_autonomous(BASE_DIR, session_key)
            or engine.conversation_scheduler.group(session_key).cooldown_until > now):
        audit_entry(BASE_DIR,entry,'cancelled',reason='autonomous_unavailable')
        engine.trigger_queue.popleft()
        return
    if mode==COLD_MODE and cold_store(str(BASE_DIR)).due(cfg,now)!=session_key:
        audit_entry(BASE_DIR,entry,'cancelled',reason='cold_opportunity_expired')
        engine.trigger_queue.popleft()
        return
    adaptive_turn = mode in ("上下文积累判断", "相关话题续聊", "被@后监听窗口")
    if adaptive_turn:
        decision = engine.conversation_scheduler.decide(session_key, cfg, now)
        if not adaptive_enabled(cfg) or not decision.ready:
            audit_entry(BASE_DIR,entry,'cancelled',reason='decision_not_ready')
            engine.trigger_queue.popleft()
            return
        mode = "即时唤醒(被@/私聊)" if decision.mode == "mention" else (
            "被@后监听窗口" if decision.mode == "listening" else "相关话题续聊" if decision.mode == "continuation" else "上下文积累判断")
        score = decision.score
        selection = entry.get('continuation_selection')
        if mode == '被@后监听窗口' and selection is not None:
            state = engine.conversation_scheduler.group(session_key)
            if (selection['through'] != state.listen_cutoff
                    or selection.get('listen_trigger_id') != state.listen_trigger_id
                    or now > state.listen_due):
                audit_entry(BASE_DIR,entry,'cancelled',reason='window_expired')
                engine.trigger_queue.popleft()
                engine.clear_unread(session_key, through=selection['through'])
                engine.conversation_scheduler.reject_window_batch(session_key, selection['through'], now)
                return
            score = selection['score']  # Reuse the reviewed batch; only its lifecycle is rechecked.
    # Scored adaptive batches/continuations already carry the extra social cost.
    # Listening, cold-topic and legacy opportunities pass the same final gate.
    if (session_key == 'group:1142791485379513241'
            and mode != '即时唤醒(被@/私聊)'
            and mode not in ('上下文积累判断', '相关话题续聊', '被@后监听窗口')):
        from hakimi.social.social_behavior import behavior_for, group_initiative_score
        rows = entry.get('continuation_selection', {}).get('rows', []) if isinstance(entry.get('continuation_selection'), dict) else []
        rows = rows or engine.context_windows.get(session_key, [])
        author = next((row.get('user_id') for row in reversed(rows)
                       if row.get('user_id') and row.get('user_id') != 'SELF'), None)
        social = behavior_for(BASE_DIR, author, session_key, now=now, config=cfg)
        baseline = (.45 if mode == '被@后监听窗口' else
                    engine.get_group_weight_info(session_key.split(':', 1)[1], cfg)['min_score'])
        # Legacy warm/exploration paths may have waived their old score floor.
        # Preserve that original eligibility at unit gain.
        adjusted = group_initiative_score(session_key, max(score, baseline), social)
        if adjusted < baseline:
            audit_entry(BASE_DIR,entry,'cancelled',reason='participation_score_low')
            engine.trigger_queue.popleft()
            return
        score = adjusted
    from hakimi.bridge.reply_format import group_reply_rules
    probability = group_reply_rules(session_key, cfg).get('proactive_probability', 1)
    if mode not in ("即时唤醒(被@/私聊)", "被@后监听窗口", "相关话题续聊") and probability < 1:
        # Sample once per queued opportunity, never once per polling tick.
        if entry.setdefault('initiative_sample', random.random()) >= probability:
            if mode == COLD_MODE:
                store = cold_store(str(BASE_DIR)); skipped = 'probability-' + uuid.uuid4().hex
                store.offered(session_key, skipped, now)
                store.progress({'trigger_id': skipped, 'status': 'completed', 'outcome': 'no_reply'}, now)
            audit_entry(BASE_DIR,entry,'cancelled',reason='participation_probability')
            engine.trigger_queue.popleft()
            engine.clear_unread(session_key)
            return
    if mode == "低频尝试参与":
        timestamps = engine.recent_msg_timestamps.get(session_key, ())
        last_human_at = timestamps[-1] if timestamps else 0
        if (not cfg.get("lurker_schedule", {}).get("enable_random_window_peek", True)
                or not exploration_context_allowed(session_key, last_human_at, now, cfg)
                or engine.unread_counts.get(session_key, 0) <= 0
                or now < engine.cooldown_until.get(session_key, 0)
                or not study_reply_allowed(session_key, now, cfg, BASE_DIR,
                                           engine.last_replied_at.get(session_key, 0))):
            audit_entry(BASE_DIR,entry,'cancelled',reason='exploration_expired')
            engine.trigger_queue.popleft()
            print(f"   [低频机会过期] {session_key} 已无新鲜未读消息或正在冷却，保持静默")
            return
        # Reserve only when actually dispatching. If publish temporarily refuses,
        # the queued marker prevents a retry from charging the same attempt twice.
        if "exploration_reserved_at" not in entry:
            if not reserve_exploration(session_key, last_human_at, now, cfg, BASE_DIR):
                audit_entry(BASE_DIR,entry,'cancelled',reason='exploration_budget')
                engine.trigger_queue.popleft()
                print(f"   [低频机会取消] {session_key} 本轮探索额度不可用，保持静默")
                return
            entry["exploration_reserved_at"] = now
    engine.last_any_wake_time = now
    LAST_WAKE_WAS_PROACTIVE = (mode == "冷场主动抛话题")
    engine.last_wake_info = {"session_key": session_key, "time": now, "mode": mode}

    kind, target_id = session_key.split(":", 1)
    recent_msgs, anchors = _mention_inputs(entry, cfg, now)
    if session_key.startswith('group:') and mode.startswith('即时唤醒') and anchors:
        from hakimi.bridge.conversation_judge import mention_context
        state=engine.conversation_scheduler.group(session_key)
        reviewed=mention_context(entry,recent_msgs,anchors,BASE_DIR,
            previous_reply=state.listen_reply,own_message_ids=list(state.own_ids),
            on_ready=engine.study_schedule_event.set)
        if reviewed is None:
            audit_entry(BASE_DIR,entry,'reviewing')
            return
        audit_entry(BASE_DIR,entry,'context_ready')
        recent_msgs=reviewed
    listening = engine.conversation_scheduler.listening_context(session_key, now) if mode == "被@后监听窗口" else None
    selection = entry.get("continuation_selection") if mode == "被@后监听窗口" else None
    if selection is not None:
        recent_msgs = [dict(row) for row in selection["rows"]] + [row for row in recent_msgs if row.get("user_id") == "SELF"][-2:]
        recent_msgs.sort(key=lambda row: row.get("received_at", now))
    if selection is not None:
        chosen = set(selection["message_ids"])
        listening["selected_message_ids"] = list(selection["message_ids"])
        recent_msgs = [row for row in recent_msgs if str(row.get("message_id", "")) in chosen or row.get("user_id") == "SELF"]
        allowed_images = set(selection.get("image_message_ids", []))
        import re as _image_re
        cleaned = []
        for row in recent_msgs:
            if row.get("user_id") != "SELF" and str(row.get("message_id", "")) not in allowed_images:
                row = dict(row)
                row.pop("image_paths", None)
                row["text"] = _image_re.sub(r"\[CQ:image,[^\]]*\]|\[(?:群友发图|表情包图片)[^\]]*\]", "", str(row.get("text", "")))
            cleaned.append(row)
        recent_msgs = cleaned
    recent_msgs, last_msg = select_context({"session_key": session_key,
        "recent_context_messages": recent_msgs,
        "listening_window": listening,
        "pending_explicit_question_ids": [str(row["message_id"]) for row in anchors]})
    from hakimi.bridge.image_initiative import image_candidates, decide as roll_image
    if adaptive_enabled(cfg):
        pending_ids = {r[2] for r in engine.conversation_scheduler.group(session_key).pending}
    else:
        unread = engine.unread_counts.get(session_key, 0)
        pending_ids = {str(r.get('message_id','')) for r in recent_msgs[-unread:]} if unread else set()
    pictures = image_candidates(session_key, mode, recent_msgs, pending_ids)
    if pictures and selection is None:
        from hakimi.social.social_behavior import behavior_for
        person = str(pictures[-1].get('user_id', ''))
        social = behavior_for(BASE_DIR, person, session_key, now=now, config=cfg)
        affinity = social.get('affinity')
        if affinity is None:
            affinity = cfg.get('user_bias', {}).get('users', {}).get(person, {}).get('affinity', 50)
        fresh_roll = 'image_roll' not in entry
        sample = roll_image(entry, affinity, random.random() if fresh_roll else 0)
        if fresh_roll:
            audit = dict(sample, at=now, session=session_key, source_message_id=str(pictures[-1].get('message_id','')))
            try:
                with (BASE_DIR/'state/image_initiative.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps(audit,ensure_ascii=False)+'\n')
            except OSError:
                pass
        if not sample['accepted']:
            audit_entry(BASE_DIR,entry,'cancelled',reason='image_probability')
            engine.trigger_queue.popleft()
            engine.clear_unread(session_key)
            return
    if selection is not None and not any(row.get("user_id") != "SELF" for row in recent_msgs):
        audit_entry(BASE_DIR,entry,'cancelled',reason='empty_selection')
        engine.trigger_queue.popleft()
        return
    uid = str(last_msg.get("user_id", ""))
    u_info = cfg["user_bias"].get("users", {}).get(uid, {
        "nickname": last_msg.get("nickname", ""),
        "affinity": cfg["user_bias"].get("default_affinity", 50),
        "attitude": "普通群友",
    })
    gw_info = group_theme_info or (engine.get_group_weight_info(target_id, cfg) if kind == "group" else {})

    trigger_payload = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "wake_mode": mode,
        "bot_persona": cfg["bot_profile"]["persona"],
        "session_key": session_key,
        "message_type": kind,
        "group_id": int(target_id) if kind == "group" and target_id.isdigit() else target_id,
        "group_theme_and_weight": gw_info,
        "interest_score": round(score, 3),
        "decision_reasons": reasons,
        "last_sender_profile": u_info,
        "recent_context_messages": recent_msgs,
        "reply_target_message_id": str(last_msg.get("message_id", "")),
    }
    if mode==COLD_MODE:
        trigger_payload['cold_topic_request']=cold_store(str(BASE_DIR)).context(session_key,cfg)
        trigger_payload['is_proactive']=True
        trigger_payload['recent_context_messages']=recent_msgs[-4:]
        trigger_payload['reply_target_message_id']=''
    memory_nodes=[]
    if mode!='被@后监听窗口':
        try:
            from hakimi.memory.memory_store import store
            memory_nodes=store(str(BASE_DIR)).topic_signature(session_key,str(last_msg.get('text','')))
        except (OSError,sqlite3.Error):
            pass
    if anchors:
        trigger_payload["pending_explicit_question_ids"] = [str(row["message_id"]) for row in anchors]
    if listening:
        trigger_payload["listening_window"] = listening
    if mode in ('被@后监听窗口', '相关话题续聊'):
        trigger_payload['conversation_lane'] = reply_lane
    published = coordinator.publish(trigger_payload)
    if not published["accepted"]:
        if published.get('reason') == 'source_already_answered':
            engine.trigger_queue.popleft()
            through = selection['through'] if selection is not None else last_msg.get('received_at')
            if through is not None:
                if selection is not None:
                    engine.conversation_scheduler.reject_window_batch(session_key, through, now)
                else:
                    engine.conversation_scheduler.consume(session_key, through=through)
            return
        if published.get('reason') == 'user_reply_limit':
            engine.trigger_queue.popleft()
            engine.clear_unread(session_key)
            engine.conversation_scheduler.stop_listening(session_key)
        return
    if mode==COLD_MODE:
        cold_store(str(BASE_DIR)).offered(session_key,published['trigger_id'],now)
    window_state = engine.conversation_scheduler.group(session_key)
    if selection is not None:
        # All messages in this frozen batch have already been considered by DSH.
        window_state.context_high_water = max(window_state.context_high_water, selection['through'])
    if entry.get('mention_review'):
        window_state.context_high_water=max(window_state.context_high_water,entry['mention_review']['through'])
    if adaptive_enabled(cfg):
        engine.conversation_scheduler.offered_turn(session_key, now, published["trigger_id"], messages=recent_msgs, wake_mode=mode,memory_nodes=memory_nodes,reply_author=str(last_msg.get("user_id", "")),conversation_lane=published.get("conversation_lane", ""))
    known = {str(row.get("message_id", "")) for row in window_state.window_messages}
    window_state.window_messages.extend(dict(row) for row in engine.context_windows.get(session_key, ())
        if row.get("user_id") != "SELF" and row.get("received_at", 0) > window_state.context_high_water
        and str(row.get("message_id", "")) not in known)
    engine.trigger_queue.popleft()
    trigger_payload["trigger_id"] = published["trigger_id"]
    temp = PENDING_TRIGGER.with_suffix(".tmp")
    temp.write_text(json.dumps(trigger_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(PENDING_TRIGGER)
    consumed_through = selection["through"] if selection is not None else max([0, *[float(row.get("received_at", 0)) for row in recent_msgs if row.get("user_id") != "SELF"]])
    if entry.get('mention_review'):
        consumed_through=max(consumed_through,entry['mention_review']['through'])
    engine.clear_unread(session_key, through=consumed_through)
    print(f"   🚀 [信标已派发给 Flash] -> {session_key} ({mode}, 剩余排队: {len(engine.trigger_queue)})")


def conversation_window_rows(session, started, through, cached=()):
    """Read the whole durable time window; runtime cache survives inbox rotation."""
    by_id = {}
    with HISTORY_IO_LOCK:
        try:
            with INBOX_LOG.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if (isinstance(row, dict) and row.get("session_key") == session
                            and row.get("user_id") != "SELF"
                            and started < float(row.get("received_at", 0)) <= through):
                        by_id[str(row.get("message_id", ""))] = row
        except OSError:
            pass
    for row in cached:
        if started < float(row.get("received_at", 0)) <= through and row.get("user_id") != "SELF":
            by_id[str(row.get("message_id", ""))] = dict(row)
    cfg = load_config()
    from hakimi.bridge.interaction_policy import ignored, ignored_text
    return sorted([row for row in by_id.values() if not ignored(row.get("user_id"), BASE_DIR, cfg, session=session)
                   and not ignored_text(row.get("text", ""), cfg)], key=lambda row: row.get("received_at", 0))


async def process_incoming_event(event: dict):
    if event.get("post_type") == "meta_event":
        self_id = event.get("self_id")
        if self_id and not ONLINE_STATUS_FILE.exists():
            ONLINE_STATUS_FILE.write_text(
                json.dumps({"self_qq": self_id, "connected_at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2),
                encoding="utf-8",
            )
            print(f"★ 已成功识别并绑定登录的 QQ 账号: {self_id}")
        return

    if event.get("post_type") != "message":
        return

    cfg = load_config()
    user_id = str(event.get("user_id", ""))
    self_id = str(event.get("self_id", cfg["bot_profile"].get("self_qq", bot_identity.SELF_QQ)))
    ignored_users = {str(uid) for uid in cfg["bot_profile"].get("ignored_user_ids", [])}
    from hakimi.bridge.interaction_policy import known_ai
    if user_id == self_id or user_id in ignored_users and not known_ai(user_id,cfg):
        return

    msg_type = event.get("message_type", "group")
    group_id = str(event.get("group_id", "")) if msg_type == "group" else "private"
    session_key = f"{msg_type}:{group_id if msg_type == 'group' else user_id}"
    if not session_is_allowed(session_key, cfg):
        return
    if msg_type=='group' and known_ai(user_id,cfg):
        from hakimi.bridge.bot_registry import platform
        platform(BASE_DIR,user_id,event.get('sender',{}).get('nickname',''),session_key)
    from hakimi.bridge.interaction_policy import ignored
    if ignored(user_id,BASE_DIR,cfg,bot_cooldown=False,session=session_key):return
    message_id = event.get("message_id")
    if event.get('_voice_attachments') and not event.get('_voice_done'):
        from hakimi.voice.store import enqueue
        from hakimi.bridge.interaction_policy import ignored_text
        native_raw=str(event.get('raw_message',''))
        if ignored_text(native_raw,cfg):return
        enqueue(BASE_DIR,event,priority=msg_type=='private' or direct_attention(event,cfg))
        return
    if message_id is not None:
        event_key = (session_key, str(message_id))
        if event_key in RECENT_EVENT_IDS:
            RECENT_EVENT_IDS.move_to_end(event_key)
            return
        RECENT_EVENT_IDS[event_key] = event.get('_voice_received_at',time.time()) if event.get('_voice_done') else time.time()
        if len(RECENT_EVENT_IDS) > 500:
            RECENT_EVENT_IDS.popitem(last=False)
    nickname = event.get("sender", {}).get("card") or event.get("sender", {}).get("nickname", f"QQ_{user_id}")
    raw_text = event.get("raw_message", "").strip()
    from hakimi.bridge.interaction_policy import ignored_text
    if ignored_text(raw_text,cfg):return
    # Image downloads / get_msg may take seconds; do not block WS acknowledgements.
    image_paths = []
    cleaned = await asyncio.to_thread(
        format_clean_message, raw_text, self_id,
        message_id if message_id is not None else int(time.time() * 1000), engine.msg_lookup.copy(),
        image_paths, session_key,
    )
    if event.get('_voice_ref'):
        cleaned += " [语音消息：自动转写原文仅在本会话可读，voice.read message_id="+str(message_id)+"]"
    if event.get('_voice_ref'):
        # Commit the single semantic source receipt before a fast model can
        # claim the wake.  No transcript is copied into the general memory.
        try:
            from hakimi.memory.memory_store import store as voice_memory_store
            from hakimi.voice.store import acknowledge as acknowledge_voice
            voice_source={'session_key':session_key,'message_id':str(message_id),'user_id':user_id,'nickname':nickname,'received_at':event['_voice_received_at'],'text':cleaned,'channel':'voice','direct_targets':sorted(set(re.findall(r'\[CQ:at,qq=([0-9]+)\]',raw_text))),'self_id':str(self_id)}
            native_ref=re.search(r'\[CQ:reply,id=(-?[0-9]{1,20})\]',raw_text)
            voice_source['reply_to']=native_ref[1] if native_ref else ''
            await asyncio.to_thread(voice_memory_store(str(BASE_DIR)).observe,voice_source)
            acknowledge_voice(BASE_DIR,session_key,str(message_id))
        except (OSError,sqlite3.Error):
            RECENT_EVENT_IDS.pop((session_key,str(message_id)),None)
            return {'voice_deferred':True}
    memory_nodes=[]
    state=engine.conversation_scheduler.groups.get(session_key)
    if (state and state.last_reply and 0<=time.time()-state.last_reply<=300
            and not state.listen_round and not direct_attention(event,cfg)):
        try:
            from hakimi.memory.memory_store import store
            memory_nodes=await asyncio.to_thread(store(str(BASE_DIR)).topic_signature,session_key,cleaned)
        except (OSError,sqlite3.Error):
            pass
    instant_wake, reasons, clean_text = engine.record_and_score(event, cfg, cleaned, image_paths,memory_nodes)
    if not clean_text and not instant_wake and reasons:
        return
    if cold_settings(cfg).get('enable',False):
        try:
            reference=re.search(r'\[CQ:reply,id=(-?\d+)\]',raw_text)
            cold_store(str(BASE_DIR)).on_message(session_key,clean_text,user_id,message_id,cfg,time.time(),
                                               reference[1] if reference else '')
        except (OSError,sqlite3.Error) as exc:
            report_fault(BASE_DIR,'cold_topic_memory',exc)

    try:
        record_interaction(raw_text, session_key, user_id, message_id, BASE_DIR, self_id=self_id)
    except (OSError, ValueError) as exc:
        report_fault(BASE_DIR, "feeding", exc)

    with HISTORY_IO_LOCK, open(INBOX_LOG, "a", encoding="utf-8") as f:
        direct_targets = sorted(set(re.findall(r"\[CQ:at,qq=([0-9]+)\]", raw_text)))
        reply_ref = re.search(r"\[CQ:reply,id=(-?[0-9]{1,20})\]", raw_text)
        history_row = {
            "time": time.strftime("%H:%M:%S"),
            "received_at": RECENT_EVENT_IDS.get((session_key, str(message_id)), time.time()),
            "session_key": session_key,
            "message_id": event.get("message_id"),
            "user_id": user_id,
            "nickname": nickname,
            "text": clean_text or raw_text,
            "image_paths": list(dict.fromkeys(image_paths)),
            "direct_targets": direct_targets,
            "reply_to": reply_ref.group(1) if reply_ref else "",
            "self_id": str(self_id),
        }
        if event.get('_voice_ref'):
            history_row.update(voice_ref=str(message_id),channel='voice')
        f.write(json.dumps(history_row, ensure_ascii=False) + "\n")

    unread = engine.unread_counts.get(session_key, 0)
    score = engine.window_interest_score.get(session_key, 0.0)
    print(f"[{time.strftime('%H:%M:%S')}] {session_key} (未读:{unread} 兴趣:{score:.2f}) | 消息:{message_id} 文本:{len(clean_text or raw_text)}字 图片:{len(image_paths)}")

    if instant_wake and (not event.get('_voice_done') or time.time()-event['_voice_received_at']<=210):
        enqueue_wake_beacon(session_key, "即时唤醒(被@/私聊)", score, reasons, cfg)
    else:
        received_at = RECENT_EVENT_IDS.get((session_key, str(message_id)), time.time())
        if not adaptive_enabled(cfg):
            engine.arm_study_attention(session_key, cfg, received_at, base=BASE_DIR)
    # Observe + wake publication must stay atomic on the event loop. Yielding
    # between them can let the scheduler publish the same mention twice.
    try:
        from hakimi.memory.memory_store import store
        if not event.get('_voice_ref'):
            await asyncio.to_thread(store(str(BASE_DIR)).observe, history_row)
    except (OSError, sqlite3.Error) as exc:
        report_fault(BASE_DIR, "memory_index", exc)
    return {"handled":True}


async def _wait_for_peek_signal(timeout):
    """Schedule changes only recalculate sleep; they are not fast/model wakes."""
    fast = asyncio.create_task(engine.fast_peek_event.wait())
    changed = asyncio.create_task(engine.study_schedule_event.wait())
    try:
        done, _ = await asyncio.wait({fast, changed}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if fast in done:
            return "fast"
        return "schedule" if changed in done else "timeout"
    finally:
        for task in (fast, changed):
            if not task.done():
                task.cancel()
        await asyncio.gather(fast, changed, return_exceptions=True)


async def random_window_peek_loop():
    while True:
        if adaptive_enabled(load_config()):
            await adaptive_conversation_loop()
        else:
            await legacy_random_window_peek_loop()


async def cold_topic_allowed(session):
    return False  # The active official adapter supplies its live permission check.


async def maybe_schedule_cold_topic(cfg,cold):
    if not cold or not ACTIVE_WS_CLIENTS:
        return
    try:
        candidate=cold.due(cfg,time.time())
        if not candidate:
            return
        if not await cold_topic_allowed(candidate):
            cold.permission_denied(candidate,time.time())
            return
        fresh=load_config()
        scheduler=engine.conversation_scheduler
        if (scheduler.choose(fresh,time.time())[0] is not None or any(
                scheduler.decide(key,fresh,time.time()).mode=='mention' for key in list(scheduler.groups))):
            return  # A foreground message may have arrived during the permission request.
        if cold.due(fresh,time.time())==candidate:
            info=fresh.get('group_weights',{}).get(candidate.split(':',1)[1],{})
            enqueue_wake_beacon(candidate,COLD_MODE,.5,['本群约20小时未收新消息，按历史尝试短话题'],fresh,info)
    except (OSError,sqlite3.Error) as exc:
        report_fault(BASE_DIR,'cold_topic_memory',exc)


async def relationship_review_loop():
    """One bounded background lane. Event/due timers, never a speaking lock."""
    from hakimi.memory import post_reply_review, impression_events
    # No network request in recover; restored live windows remain open.
    try:
        await asyncio.to_thread(post_reply_review.recover,BASE_DIR,engine.conversation_scheduler,time.time())
    except Exception:
        report_fault(BASE_DIR,'post_reply_review',code='REVIEW_RECOVERY_FAILED')
    maintenance_due=time.time()
    while True:
        engine.reply_review_event.clear()
        maintenance_idle=False
        try:
            await asyncio.to_thread(post_reply_review.tick,BASE_DIR)
            now=time.time()
            retry_due=await asyncio.to_thread(post_reply_review.next_due,BASE_DIR,now,maintenance_only=True)
            if now>=maintenance_due or (retry_due is not None and retry_due<=now):
                result=await asyncio.to_thread(impression_events.tick,BASE_DIR)
                maintenance_idle=result.get('status')=='idle'
                # Keep the supervisor's existing compact status consumer intact.
                target=project_root(BASE_DIR)/'state'/'terra_watch'/'impression_results.json'
                target.parent.mkdir(parents=True,exist_ok=True)
                temp=target.with_suffix('.tmp')
                temp.write_text(json.dumps(dict(at=now,**result),ensure_ascii=False),encoding='utf-8')
                temp.replace(target)
                maintenance_due=time.time()+300
            due=await asyncio.to_thread(post_reply_review.next_due,BASE_DIR,time.time())
            delay=max(.1,min(maintenance_due-time.time(),(due-time.time()) if due is not None else 300))
            if maintenance_idle and delay<1:
                window_due=await asyncio.to_thread(post_reply_review.next_due,BASE_DIR,time.time(),window_only=True)
                if window_due is None or window_due>time.time():
                    delay=30
        except Exception:
            report_fault(BASE_DIR,'post_reply_review',code='REVIEW_LOOP_FAILED')
            delay=30
        try:
            await asyncio.wait_for(engine.reply_review_event.wait(),timeout=max(.1,delay))
        except asyncio.TimeoutError:
            pass


async def adaptive_conversation_loop():
    reviews = {}
    print("[统一会话调度] 本地积累、相关续聊与活跃群退让已启用")
    while True:
        cfg = load_config()
        if not adaptive_enabled(cfg):
            return
        engine.study_schedule_event.clear()
        scheduler = engine.conversation_scheduler
        lane_coordinator = coordinator_for(BASE_DIR, cfg)
        snapshot = lane_coordinator.snapshot()
        for lane_snapshot in lane_coordinator.snapshots():
            scheduler.outcome(lane_snapshot, time.time())
        cold=None
        if cold_settings(cfg).get('enable',False):
            try:
                cold=cold_store(str(BASE_DIR))
                cold.bootstrap(cfg,time.time())
                cold.progress(snapshot,time.time())
            except (OSError,sqlite3.Error) as exc:
                report_fault(BASE_DIR,'cold_topic_memory',exc)
                cold=None
        if engine.trigger_queue or delivery_in_progress():
            for key in list(scheduler.groups):
                if scheduler.groups[key].pending:
                    scheduler.audit_decision(key, scheduler.decide(key, cfg, time.time()), time.time(), "busy")
            flush_trigger_queue_if_waiting()
            # All explicit mentions remain in the existing priority queue.
            # Keep accumulated inputs while a single-owner turn is in progress.
            try:
                mention_waits = [decision.wait for key in list(scheduler.groups)
                                 if (decision := scheduler.decide(key, cfg, time.time())).mode == "mention"
                                 and not decision.ready]
                await asyncio.wait_for(engine.study_schedule_event.wait(),
                                       timeout=max(.05, min([5, *mention_waits])))
            except asyncio.TimeoutError:
                pass
            continue
        # Background refinement must not hold up another group's explicit question.
        for key in list(reviews):
            state = scheduler.group(key)
            if not state.listen_round or (state.listen_cutoff or state.listen_due) != reviews[key]['through']:
                reviews.pop(key)
        waiting = {key for key, entry in reviews.items()
                   if entry.get('continuation_review') and not entry['continuation_review']['task'].done()
                   and scheduler.decide(key, cfg, time.time()).mode != 'mention'}
        for key in list(scheduler.groups):
            pending_decision = scheduler.decide(key, cfg, time.time())
            pending_mode = ('即时唤醒(被@/私聊)' if pending_decision.mode == 'mention' else
                            '被@后监听窗口' if pending_decision.mode == 'listening' else
                            '相关话题续聊' if pending_decision.mode == 'continuation' else '上下文积累判断')
            if lane_coordinator.available_lane({'session_key': key, 'wake_mode': pending_mode,
                    'conversation_lane': scheduler.group(key).reply_lane}) is None:
                waiting.add(key)
        session, decision = scheduler.choose(cfg, time.time(), exclude=waiting)
        if session:
            scheduler.audit_decision(session, decision, time.time(), "selection")
            mode = "即时唤醒(被@/私聊)" if decision.mode == "mention" else (
                "被@后监听窗口" if decision.mode == "listening" else "相关话题续聊" if decision.mode == "continuation" else "上下文积累判断")
            details = decision.metadata()
            reasons = [f"统一评分={details['score']}, 相关={details['relevance']}, 依据={details['basis']}, 活跃={details['activity']}, 参与负担={details['pressure']}"]
            selection = None
            from hakimi.transport.group_capabilities import can_autonomous
            if decision.mode != "mention" and not can_autonomous(BASE_DIR, session):
                scheduler.stop_listening(session)
                scheduler.consume(session)
                continue
            if decision.mode == "listening":
                state = scheduler.group(session)
                through = state.listen_cutoff or state.listen_due
                review = reviews.get(session)
                if review is None:
                    rows = await asyncio.to_thread(conversation_window_rows, session, state.listen_started, through, list(state.window_messages))
                    review = reviews[session] = {'through': through, 'rows': rows}
                rows = review['rows']
                from hakimi.bridge.conversation_judge import continuation_context
                selected = continuation_context(review, rows, state.listen_reply, BASE_DIR,
                    context=list(engine.context_windows.get(session,())), own_message_ids=list(state.own_ids),
                    source_message_ids=state.listen_source_ids, reply_trigger_id=state.listen_trigger_id, session_key=session)
                if review.get('continuation_review') and not state.listen_review_started:
                    state.listen_review_started = True
                    state.listen_next_check = time.time()+15
                    scheduler.save()
                if selected is None:
                    await asyncio.sleep(.5)
                    continue
                reviews.pop(session, None)
                # New @ messages received while the judge worked retain priority.
                if any(row[5] and row[0] > through for row in state.pending):
                    continue
                from hakimi.bridge.image_initiative import image_candidates, probability
                from hakimi.bridge.conversation_scheduler import actor, features, similarity
                from hakimi.social.social_behavior import behavior_for
                accepted_images = []
                for image_row in image_candidates(session, mode, rows, {str(row.get("message_id", "")) for row in rows}):
                    directed=image_row.get('direct_targets') or image_row.get('reply_to') or re.search(r'\[CQ:(?:at|reply),',str(image_row.get('text','')))
                    if directed and str(image_row.get('message_id','')) not in selected:continue
                    person = str(image_row.get("user_id", ""))
                    social = behavior_for(BASE_DIR, person, session, now=time.time(), config=cfg)
                    affinity = social.get("affinity")
                    if affinity is None:
                        affinity = cfg.get("user_bias", {}).get("users", {}).get(person, {}).get("affinity", 50)
                    related = similarity(features(image_row.get("text", "")), state.anchor | state.source)
                    relevance_probability = min(1.0, max(.65 if actor(person) == state.first_reply_author else .15, related))
                    if random.random() < probability(affinity)*relevance_probability:
                        accepted_images.append(str(image_row.get("message_id", "")))
                reviewed_ids = tuple(selected)
                selected = list(dict.fromkeys(selected+accepted_images))
                selection = {"message_ids": selected, "reviewed_message_ids": list(reviewed_ids),
                             "listen_trigger_id": state.listen_trigger_id,
                             "image_message_ids": accepted_images, "through": through,
                             "rows": [dict(row) for row in rows if str(row.get("message_id", "")) in set(selected)]}
                decision = scheduler.score_window_messages(session, cfg, time.time(), selection['rows'], reviewed_message_ids=reviewed_ids)
                if not selected:
                    from dataclasses import replace
                    decision = replace(decision, basis='review_failed' if review.get('continuation_review', {}).get('failed') else 'review_empty')
                selection['score'] = decision.score
                scheduler.audit_decision(session, decision, time.time(), 'refined_selection',
                    selection_counts={'candidates': len(rows), 'reviewed': len(reviewed_ids),
                                      'images': len(accepted_images), 'selected': len(selection['rows'])})
                if not selected or not decision.ready:
                    scheduler.reject_window_batch(session, through, time.time())
                    continue
                details = decision.metadata()
                reasons = [f"梳理后评分={details['score']}, 相关={details['relevance']}, 依据={details['basis']}, 活跃={details['activity']}, 参与负担={details['pressure']}"]
            enqueue_wake_beacon(session, mode, decision.score, reasons, cfg, continuation_selection=selection)
            # If a worker is unavailable, do not spin on a still-ready group.
            await asyncio.sleep(.5)
            continue
        await maybe_schedule_cold_topic(cfg,cold)
        waits = [scheduler.decide(key, cfg, time.time()).wait for key in list(scheduler.groups)
                 if scheduler.groups[key].pending or scheduler.groups[key].listen_round]
        for key in list(scheduler.groups):
            if scheduler.groups[key].pending:
                scheduler.audit_decision(key, scheduler.decide(key, cfg, time.time()), time.time(), "selection")
        delay = max(.5, min([1.0 if reviews else 30.0, *waits]))
        try:
            await asyncio.wait_for(engine.study_schedule_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


async def legacy_random_window_peek_loop():
    """支持【打盹长周期】与【余温/爆发快速唤醒】的双态动态巡检协程"""
    next_regular_at = time.monotonic() if getattr(engine, "recovered_unread_pending", False) is True else None
    engine.recovered_unread_pending = False
    while True:
        cfg = load_config()
        if adaptive_enabled(cfg):
            return
        lurk_cfg = cfg.get("lurker_schedule", {})
        min_s = lurk_cfg.get("min_interval_seconds", 180)
        max_s = lurk_cfg.get("max_interval_seconds", 480)
        # 深夜 00:00~08:00 拉长普通周期；指定学习群的新消息另有150秒检查上界。
        cur_hour = time.localtime().tm_hour
        if 0 <= cur_hour < 8:
            min_s = max(360, min_s * 2)
            max_s = max(900, int(max_s * 1.8))
        if next_regular_at is None:
            wait_s = random.randint(min_s, max_s)
            next_regular_at = time.monotonic() + wait_s
            print(f"   [哈基米动态巡检器] 常态打盹窗口 {wait_s}s ({wait_s/60:.1f}m)，若遇热聊余温/消息爆发将提前秒级唤醒...")

        engine.fast_peek_event.clear()
        engine.study_schedule_event.clear()
        study_due = engine.next_study_attention_deadline(cfg, base=BASE_DIR)
        effective_due = min(next_regular_at, study_due) if study_due is not None else next_regular_at
        woken_fast = False
        remaining = max(0, effective_due - time.monotonic())
        if remaining > 0:
            signal = await _wait_for_peek_signal(remaining)
            if signal == "schedule":
                continue
            if signal == "fast":
                woken_fast = True
                # A study receipt may arrive together with a hot-group signal.
                # Respect its newly shortened deadline before applying fast delay.
                study_due = engine.next_study_attention_deadline(cfg, base=BASE_DIR)
                if study_due is not None:
                    effective_due = min(effective_due, study_due)
                fast_delay = min(random.randint(6, 9), max(0, effective_due - time.monotonic()))
                print(f"   ⚡ [动态提速] 检测到余温接话或群聊爆发，将在 {fast_delay}s 后提前窥屏！")
                await asyncio.sleep(fast_delay)
        engine.check_skip_and_expired_feedback()
        cfg = load_config()
        lurk_cfg = cfg.get("lurker_schedule", {})
        if not lurk_cfg.get("enable_random_window_peek", True):
            next_regular_at = None
            engine.clear_study_attention()
            continue
        # A configuration change during sleep may remove the early slot while
        # leaving ordinary participation authorized. Preserve the original
        # ordinary deadline instead of accelerating an unrelated group.
        study_due = engine.next_study_attention_deadline(cfg, base=BASE_DIR)
        effective_due = min(next_regular_at, study_due) if study_due is not None else next_regular_at
        regular_due = time.monotonic() >= effective_due
        if not regular_due and not woken_fast:
            continue
        if coordinator_for(BASE_DIR, cfg).snapshot().get("status") in ("queued", "primary_active", "fallback_active") or engine.trigger_queue:
            flush_trigger_queue_if_waiting()
            # Preserve an overdue ordinary slot while the current turn finishes,
            # but do not spin or enqueue extra model work in the meantime.
            await asyncio.sleep(5)
            continue

        fast_only = woken_fast and not regular_due
        if regular_due:
            next_regular_at = None
        sk, score, reasons = engine.pick_session_for_random_peek(cfg, fast_only=fast_only)
        if not fast_only:
            # Unselected eligible groups retain their due slot. Invalid/no-match
            # deadlines are consumed so they cannot create a zero-delay loop.
            engine.clear_study_attention(sk)
        if sk:
            mode = "低频尝试参与" if reasons and reasons[0].startswith("低频尝试参与") else "随机时间窗窥屏"
            enqueue_wake_beacon(sk, mode, score, reasons, cfg)
        elif not fast_only:
            pro_sk, pro_info, pro_reasons = engine.pick_group_for_proactive_topic(cfg)
            if pro_sk:
                enqueue_wake_beacon(pro_sk, "冷场主动抛话题", float(pro_info.get("weight", 1.0)), pro_reasons, cfg, pro_info)
            else:
                idle_m = int((time.time() - engine.last_any_wake_time) / 60)
                thresh_m = int(engine.next_proactive_threshold / 60)
                print(f"   [{time.strftime('%H:%M:%S')}] 动态巡检：暂无达标消息 (静默 {idle_m}/{thresh_m}m)，继续打盹~")


def session_is_allowed(session_key: str, cfg: dict) -> bool:
    try:
        kind, target_id = session_key.split(":", 1)
    except (ValueError, AttributeError):
        return False
    profile = cfg.get("bot_profile", {})
    if kind == "private":
        return bool(profile.get("allow_private_chat", True))
    if kind != "group":
        return False
    active = {str(gid) for gid in profile.get("active_groups", [])}
    ignored = {str(gid) for gid in profile.get("ignored_groups", [])}
    return target_id not in ignored and (not active or target_id in active)


async def send_via_onebot(session_key: str, text: str, timeout: float = 12) -> dict:
    if not ACTIVE_WS_CLIENTS:
        return {"status": "offline"}

    if text.startswith('[CQ:file,name=_drawing_'):
        from hakimi.tools.drawing_delivery import onebot_image
        try:
            text = onebot_image(BASE_DIR, session_key, text)
        except (OSError, ValueError):
            return {'status': 'file_error'}
    kind, target_id = session_key.split(":", 1)
    echo = "agy_" + uuid.uuid4().hex
    payload = {
        "action": "send_group_msg" if kind == "group" else "send_private_msg",
        "params": {"group_id" if kind == "group" else "user_id": int(target_id), "message": text},
        "echo": echo,
    }
    # Select a single connection. Retrying a different socket can duplicate a send.
    websocket = max(ACTIVE_WS_CLIENTS, key=ACTIVE_WS_CLIENTS.get)
    future = asyncio.get_running_loop().create_future()
    PENDING_ACKS[echo] = (future, websocket)
    try:
        await asyncio.wait_for(websocket.send(json.dumps(payload, ensure_ascii=False)), timeout=timeout)
        response = await asyncio.wait_for(future, timeout=timeout)
        if response.get("status") == "ok" and response.get("retcode") == 0:
            return {"status": "success", "message_id": (response.get("data") or {}).get("message_id")}
        if response.get("status") == "failed" or response.get("retcode") not in (None, 0):
            return {"status": "rejected"}
        return {"status": "unknown"}
    except Exception:
        # Transport errors can occur after the platform accepted the message.
        return {"status": "unknown"}
    finally:
        PENDING_ACKS.pop(echo, None)
        if not future.done():
            future.cancel()


def resolve_onebot_ack(event: dict, websocket) -> bool:
    echo = event.get("echo")
    if not isinstance(echo, str):
        return False
    waiting = PENDING_ACKS.get(echo)
    if not waiting or waiting[1] is not websocket:
        return False
    future = waiting[0]
    if not future.done():
        future.set_result(event)
    return True


async def onebot_ws_handler(websocket):
    ACTIVE_WS_CLIENTS[websocket] = time.monotonic()
    print(f"\n[OneBot 已连接] QQ 客户端已接入桥接器! 当前连接数: {len(ACTIVE_WS_CLIENTS)}")
    try:
        async for raw_msg in websocket:
            try:
                event = json.loads(raw_msg)
                if not isinstance(event, dict) or resolve_onebot_ack(event, websocket):
                    continue
                if event.get("post_type") in ("message", "meta_event"):
                    try:
                        INCOMING_EVENTS.put_nowait(event)
                    except asyncio.QueueFull:
                        print("[接收队列已满] 本次事件未处理，请检查积压；未自动发送回复。")
            except Exception as e:
                print(f"解析 OneBot 消息出错: {e}")
    except websockets.ConnectionClosed:
        pass
    finally:
        ACTIVE_WS_CLIENTS.pop(websocket, None)
        for future, owner in list(PENDING_ACKS.values()):
            if owner is websocket and not future.done():
                future.set_result({"status": "unknown"})
        if not ACTIVE_WS_CLIENTS:
            ONLINE_STATUS_FILE.unlink(missing_ok=True)
        print("[OneBot 断开] QQ 客户端连接已断开。")


async def incoming_event_loop():
    while True:
        event = await INCOMING_EVENTS.get()
        try:
            await process_incoming_event(event)
        except Exception as exc:
            report_fault(BASE_DIR, "incoming_event_loop", exc)
            print(f"[接收处理失败] {type(exc).__name__}")
        finally:
            INCOMING_EVENTS.task_done()


async def watch_simulation_and_outbox():
    guard = DeliveryGuard(DELIVERY_STATE_FILE, OUTBOX_FILE, load_config().get("delivery_policy", {}))
    sim_pos = SIM_INPUT_FILE.stat().st_size if SIM_INPUT_FILE.exists() else 0

    while True:
        # 持续检查 wait_next_event.py 是否已就绪等待，若就绪则立即派发队列中的信标
        flush_trigger_queue_if_waiting()

        if SIM_INPUT_FILE.exists():
            cur_size = SIM_INPUT_FILE.stat().st_size
            if cur_size < sim_pos:
                sim_pos = cur_size
            if cur_size > sim_pos:
                with open(SIM_INPUT_FILE, "rb") as f:
                    f.seek(sim_pos)
                    while True:
                        line = f.readline()
                        if not line or not line.endswith(b"\n"):
                            break
                        sim_pos = f.tell()
                        if line.strip():
                            try:
                                event = json.loads(line.decode("utf-8"))
                                if isinstance(event, dict):
                                    INCOMING_EVENTS.put_nowait(event)
                            except (ValueError, UnicodeError, asyncio.QueueFull):
                                print("[模拟输入格式错误或接收积压] 已隔离该行。")

        cfg = load_config()
        guard.configure(cfg.get("delivery_policy", {}))
        previous_delivery_event = guard.state.get("last_event")
        message = guard.next_message()
        if message:
            session_key, text = message
            if not session_is_allowed(session_key, cfg):
                guard.reject_pending()
            elif not ACTIVE_WS_CLIENTS:
                guard.note_offline()
            else:
                first_segment = guard.pending["next_segment"] == 0
                is_proactive = guard.pending["is_proactive"]
                reply_text = " ".join(guard.pending.get("segments", []))
                trigger_id = guard.pending.get("trigger_id", "")
                # Preserve the actual tool-selected target before finish_attempt clears pending.
                actual_target_id = str(guard.pending.get("reply_target_id", ""))
                reply_source = engine.conversation_scheduler.offer_sources.get(trigger_id, {})
                target_author = reply_source.get("message_authors", {}).get(actual_target_id, "")
                if actual_target_id and not target_author:
                    target_row = engine.msg_lookup.get(actual_target_id, {})
                    if target_row.get("session_key") == session_key and target_row.get("user_id") != "SELF":
                        from hakimi.bridge.conversation_scheduler import actor
                        target_author = actor(target_row.get("user_id", ""))
                starts_conversation = reply_source.get("wake_mode") != "被@后监听窗口"
                interaction=guard.pending.get('interaction')
                receipt=f"{guard.pending.get('reply_id')}:{guard.pending['next_segment']}"
                provider=guard.pending.get('provider')
                guard.begin_attempt()
                result = await send_via_onebot(session_key, text, guard.policy["ack_timeout_seconds"])
                guard.finish_attempt(result["status"])
                if result["status"] == "success":
                    text=result.get("sent_text",text)
                    from hakimi.bridge.interaction_policy import delivered
                    delivered(session_key,receipt,interaction,first_segment,BASE_DIR,text=text,provider=provider,
                              trigger_id=trigger_id,message_id=result.get('message_id'),
                              sticker_digest=result.get('sticker_digest'))
                    sent_at = time.time()
                    engine.context_windows.setdefault(session_key, deque(maxlen=80)).append({
                        "time": time.strftime("%H:%M:%S", time.localtime(sent_at)),
                        "received_at": sent_at,
                        "message_id": result.get("message_id") or "SELF",
                        "session_key": session_key,
                        "user_id": "SELF",
                        "nickname": cfg["bot_profile"]["name"],
                        "text": text,
                    })
                    if first_segment:
                        engine.mark_replied(session_key, cfg, is_proactive=is_proactive,
                                            reply_text=reply_text, message_id=result.get("message_id"), trigger_id=trigger_id)
                        if adaptive_enabled(cfg) and starts_conversation and target_author:
                            engine.conversation_scheduler.group(session_key).first_reply_author = target_author
                    elif adaptive_enabled(cfg) and result.get("message_id") is not None:
                        engine.conversation_scheduler.group(session_key).own_ids.append(str(result["message_id"]))
                        engine.conversation_scheduler.save()
                    engine.record_reply_review(session_key,receipt,text,result.get('message_id'),trigger_id,sent_at,
                        [actual_target_id] if actual_target_id else reply_source.get('message_ids',[]))
                    if adaptive_enabled(cfg):
                        engine.conversation_scheduler.observe_reply(session_key, time.time())
                    from hakimi.bridge.bot_registry import cooling
                    bot=(interaction or {}).get('bot_target')
                    if bot and cooling(BASE_DIR,bot):
                        scheduler=engine.conversation_scheduler
                        scheduler.stop_listening(session_key,save=False)
                        group=scheduler.group(session_key)
                        group.pending=[row for row in group.pending if str(row[1])!=bot]
                        scheduler.save()
                    print(f"[发送已确认] {session_key}, {len(text)} 字符")
                else:
                    report_fault(BASE_DIR, "delivery", code="send_" + str(result["status"]))
                    print(f"[发送停止] {session_key}: {result['status']}；不会盲目重发。")

        ended = guard.state.get("last_event", {})
        if (not guard.pending and ended is not previous_delivery_event and adaptive_enabled(cfg)
                and ended.get("session_key") in engine.conversation_scheduler.groups):
            engine.conversation_scheduler.delivery_finished(ended["session_key"], time.time())
            engine.study_schedule_event.set()
        if not guard.pending and ended is not previous_delivery_event and ended.get('session_key'):
            engine.finish_reply_review(ended['session_key'],time.time(),ended.get('trigger_id',''))

        await asyncio.sleep(0.4)


def _trim_jsonl_file(file_path: Path, keep_lines: int, trigger_lines: int, max_age_sec=None) -> int:
    """当 JSONL 文件行数超过 trigger_lines 时，滚动裁剪并仅保留最后 keep_lines 行。返回清理掉的行数。"""
    if not file_path.exists():
        return 0
    try:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        kept = lines
        if max_age_sec is not None:
            kept = []
            cutoff = time.time()-max_age_sec
            for line in lines:
                try:
                    row = json.loads(line)
                    at = row.get('received_at',row.get('created_at',0))
                    if type(at) in (int,float) and at >= cutoff:
                        kept.append(line)
                except (ValueError,TypeError,AttributeError):
                    pass
        if len(lines) > trigger_lines or len(kept) < len(lines):
            kept = kept[-keep_lines:]
            removed = len(lines) - len(kept)
            file_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            return removed
    except Exception as e:
        print(f"[清理异常] 裁剪 {file_path.name} 失败: {e}")
    return 0


def perform_periodic_cleanup(
    img_max_age_sec: int = 1800,
    ntqq_pic_max_age_sec: int = 2700,
    max_history_lines: int = 600,
    max_outbox_lines: int = 50,
) -> dict:
    """
    定期清理临时聊天记录与未被选为表情包的下载图片缓存：
    - 永久保留：memory.md, style_guide.md, config.json, stickers/* (含 stickers/collected/*),
      state/qzone_posts.jsonl, state/approval_requests.jsonl
    - 清理 1：state/img_cache/* 中超过 img_max_age_sec (默认30分钟) 的临时下载图片
      （已被哈基米选为表情包的图片已复制到 stickers/collected/，不受影响）
    - 清理 2：napcat 内部 NTQQ 缓存目录 (Pic / Video / Ptt / temp) 中超过 ntqq_pic_max_age_sec (默认45分钟) 的原始缓存文件
    - 清理 3：近期聊天缓存默认保留 600 条且不超过7天；原文索引另受记忆策略约束。
      outbox 是带持久化字节检查点的发送队列，运行中不得裁剪；需停机后统一归档并迁移检查点。
    """
    now = time.time()
    deleted_img_count = 0
    deleted_img_bytes = 0
    deleted_ntqq_count = 0
    deleted_ntqq_bytes = 0

    # 1. 清理 state/img_cache 中超过 30 分钟的临时图片
    if IMG_CACHE_DIR.exists():
        for p in IMG_CACHE_DIR.iterdir():
            if p.is_file():
                try:
                    st = p.stat()
                    if now - st.st_mtime > img_max_age_sec:
                        size = st.st_size
                        p.unlink(missing_ok=True)
                        deleted_img_count += 1
                        deleted_img_bytes += size
                except Exception:
                    pass

    # 2. 清理 NapCat / NTQQ 内部缓存目录中的过期临时媒体文件
    ntqq_base = BASE_DIR / "napcat" / "userdata" / "qq_data" / str(bot_identity.SELF_QQ) / "nt_qq" / "nt_data"
    napcat_temp = BASE_DIR / "napcat" / "temp"
    for cache_root in [ntqq_base / "Pic", ntqq_base / "Video", ntqq_base / "Ptt", napcat_temp]:
        if cache_root.exists():
            for p in cache_root.rglob("*"):
                if p.is_file():
                    try:
                        st = p.stat()
                        if now - st.st_mtime > ntqq_pic_max_age_sec:
                            size = st.st_size
                            p.unlink(missing_ok=True)
                            deleted_ntqq_count += 1
                            deleted_ntqq_bytes += size
                    except Exception:
                        pass

    # 3. 滚动裁剪临时聊天记录 JSONL
    memory_policy = load_config().get('memory_policy',{})
    days = float(memory_policy.get('recent_original_days',7))
    max_history_lines = max(100,int(memory_policy.get('hot_history_lines',max_history_lines)))
    try:
        from hakimi.memory.memory_store import store
        index = store(str(BASE_DIR))
        index.sync()  # Import the final complete rows before trimming the source cache.
        index.prune(now)
    except (OSError,sqlite3.Error) as exc:
        report_fault(BASE_DIR,'memory_cleanup',exc)
    # Optional bridge-side drain; the service's --db indexer is the primary path.
    try:
        from hakimi.memory.memory_store import store
        store(str(BASE_DIR)).index_pending_embeddings(limit=8)
    except (OSError, sqlite3.Error, ValueError):
        pass
    with HISTORY_IO_LOCK:
        trimmed_history = _trim_jsonl_file(INBOX_LOG, keep_lines=max_history_lines, trigger_lines=max_history_lines + 50,
                                          max_age_sec=days*86400)
    trimmed_outbox = 0  # Do not invalidate delivery_state.json's durable cursor.
    trimmed_sim = _trim_jsonl_file(SIM_INPUT_FILE, keep_lines=20, trigger_lines=40)

    total_mb = (deleted_img_bytes + deleted_ntqq_bytes) / (1024 * 1024)
    if deleted_img_count > 0 or deleted_ntqq_count > 0 or trimmed_history > 0 or trimmed_outbox > 0:
        print(
            f"[定时清理完成] 清理未收藏临时图 {deleted_img_count} 张 + NTQQ底层缓存 {deleted_ntqq_count} 个 "
            f"(释放 {total_mb:.2f} MB) | 裁剪过期聊天记录 {trimmed_history} 条 / 发送记录 {trimmed_outbox} 条"
        )
    return {
        "deleted_img_count": deleted_img_count,
        "deleted_ntqq_count": deleted_ntqq_count,
        "freed_mb": round(total_mb, 2),
        "trimmed_history": trimmed_history,
        "trimmed_outbox": trimmed_outbox,
        "trimmed_sim": trimmed_sim,
    }


async def auto_cleanup_loop():
    """后台定时清理协程：启动时立即执行一次，之后每 20 分钟自动巡检清理一次。"""
    await asyncio.sleep(2)
    while True:
        try:
            # Directory traversal/image cleanup must not pause WS ACKs or timers.
            await asyncio.to_thread(perform_periodic_cleanup)
        except Exception as e:
            report_fault(BASE_DIR, "cleanup_loop", e)
            print(f"[定时清理异常] {e}")
        await asyncio.sleep(1200)  # 每 20 分钟执行一次


async def bridge_heartbeat():
    offline_since = 0
    while True:
        offline_since = 0 if ACTIVE_WS_CLIENTS else (offline_since or time.time())
        path = STATE_DIR / "bridge_worker.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"at": time.time(), "pid": os.getpid(), "connected": bool(ACTIVE_WS_CLIENTS),
                                    "offline_since": offline_since}), encoding="utf-8")
        temp.replace(path)
        await asyncio.sleep(5)


async def main():
    cfg = load_config()
    host = cfg["onebot"].get("ws_reverse_host", "127.0.0.1")
    port = cfg["onebot"].get("ws_reverse_port", 8080)
    # Establish the old-outbox baseline before any new generation can commit.
    DeliveryGuard(DELIVERY_STATE_FILE, OUTBOX_FILE, cfg.get("delivery_policy", {}))
    coordinator = coordinator_for(BASE_DIR, cfg)
    if PENDING_TRIGGER.exists() and coordinator.snapshot().get("status") == "idle":
        try:
            previous = json.loads(PENDING_TRIGGER.read_text(encoding="utf-8"))
            created = time.mktime(time.strptime(previous["timestamp"], "%Y-%m-%d %H:%M:%S"))
            if 0 <= time.time() - created <= 600:
                coordinator.publish(previous)
        except (OSError, ValueError, KeyError, TypeError):
            print("[旧信标未恢复] 缺少可验证的有效时间；未自动发送")

    if adaptive_enabled(cfg):
        engine.conversation_scheduler.restore(BASE_DIR, time.time())
        engine.restore_listening_unread()
        if not engine.conversation_scheduler.healthy:
            print("[统一会话调度] 调度状态异常，主动参与暂停，明确提问仍可提交")
    restored = engine.restore_recent_study_unread(cfg)
    if restored:
        print(f"[学习群未读恢复] {restored} 条新鲜真人消息进入普通巡检候选；未直接唤醒或发送")

    print("=== QQ <-> Antigravity 桥接与【动态引擎 + 多模态图片缓存 + 带确认的持久发送队列】已启动 ===")
    print(f"1. OneBot 反向 WebSocket 监听地址: ws://{host}:{port}/onebot/v11/ws")
    async with websockets.serve(onebot_ws_handler, host, port):
        await asyncio.gather(
            watch_simulation_and_outbox(),
            incoming_event_loop(),
            random_window_peek_loop(),
            auto_cleanup_loop(),
            fallback_loop(load_config),
            bridge_heartbeat(),
            relationship_review_loop(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        report_fault(BASE_DIR, "legacy_bridge", exc, code="process_crash")
        raise
