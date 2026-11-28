"""Constant-water-level rolling context and durable consolidation for Hakimi.

Maintains a steady-sized sliding dialogue window (~12 messages) per session,
preventing context cliff drops while evicting older messages into a consolidation
buffer. High-value signals (commitments, preferences, episodes) are automatically
persisted into MemoryStore, and rolling relay summaries (150~220 chars) bridge the
conversational flow seamlessly.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime
from hakimi.core.context_window import CHAT_ZONE
from hakimi.memory.text import terms

STEADY_WINDOW_LIMIT = 12
MAX_ACTIVE_TOPICS = 6
MAX_SUMMARY_CHARS = 220
JUST_SCROLLED_MAX = 3
JUST_SCROLLED_CHAR_LIMIT = 60
EVICTION_MERGE_COUNT = 4
EVICTION_MERGE_CHARS = 200

# High-value conversational signals (strictly filtered to avoid chat/question noise)
_COMMITMENT_RE = re.compile(
    r"(?:明天|后天|周末|下周|下个月)[^？?]{0,20}(?:见|去|买|吃|聊|提醒|带|发|考|开|做|约)"
    r"|不见不散|约好了?|约定|日程|按照这个安排|记得(?:提醒|带上|叫我|帮我|明天|周末|下周|买|发)|打算(?:明天|后天|周末|下周|一起去?|去)"
)
_PREFERENCE_RE = re.compile(
    r"我喜欢|我讨厌|最喜欢|最爱(?:吃|看|玩|听)|最近在(?:研究|看|学|做|写|复习|准备)|我买了|我换了|我的生日|正在做|我在做|我习惯|我经常|爱吃|不爱吃|忌口|不怎么吃|偏好|我是(?!(?:说|觉得|想|问|看|第|开玩笑|云|☁|智慧型聊天))[^\s，。！？]{1,12}(?:专业|学生|博士|硕士|本科|老师|程序员|工程师|物理|数学|计算机)"
)
_CONCLUSION_RE = re.compile(
    r"结论是|决定了|搞定了|定下来|查到了|最终确定"
)
_FORWARD_OR_SPOOF_RE = re.compile(
    r"\[群聊的聊天记录\]|&#91;群聊的聊天记录&#93;|=== 消息 |我是智慧型聊天|目前部署模型|请及时充值|Token\s*即将耗尽"
)
_QUESTION_NOISE_RE = re.compile(
    r"[？?]$|(?:你记得|记得我|我是谁|怎么看|是不是|对不对|凭什么|吗|嘛|呢)[？?]?$"
)


def strip_quote_and_markup(text: str) -> str:
    """Strip QQ quote prefixes, CQ codes, face/attachment tags, and code fences."""
    cleaned = str(text or "")
    cleaned = re.sub(r"\[引用[^\]]*?(?:消息)?(?:\(id=[^)]*\))?:\s*\".*?\"\]", " ", cleaned, flags=re.S)
    cleaned = re.sub(r"\[引用消息\(id=[^)]*\)\]", " ", cleaned)
    cleaned = re.sub(r"&#91;引用.*?&#93;", " ", cleaned, flags=re.S)
    cleaned = re.sub(r"```[\s\S]*?```", "[代码]", cleaned)
    cleaned = re.sub(r"\[(?:CQ:[^\]]+|群友发图[^\]]*|表情包图片[^\]]*)\]", " ", cleaned)
    cleaned = re.sub(r"<(?:faceType|attachmentType)[^>]*>", " ", cleaned)
    cleaned = re.sub(r"(?:^|\s)@[^\s]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def is_noisy_eviction_candidate(raw_text: str, user_id: str = "", sender_name: str = "", clean_text: str | None = None) -> bool:
    raw = str(raw_text or "")
    clean = strip_quote_and_markup(raw) if clean_text is None else str(clean_text or "").strip()
    if str(user_id or "") == "SELF" or "哈基米" in str(sender_name or ""):
        return True
    if _FORWARD_OR_SPOOF_RE.search(raw):
        return True
    if len(clean) < 8 or _QUESTION_NOISE_RE.search(clean):
        return True
    return False


def safe_session_filename(session_key: str) -> str:
    """Generate a clean, filesystem-safe filename with hash entropy."""
    clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(session_key or "default"))
    digest = hashlib.sha256(str(session_key).encode("utf-8")).hexdigest()[:8]
    return f"{clean}_{digest}.json"


def get_rolling_state_dir(base: Path | str) -> Path:
    p = Path(base) / "state" / "rolling_context"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_rolling_state(base: Path | str, session: str) -> dict:
    """Load persistent rolling context state for a session, or return initial state."""
    state_dir = get_rolling_state_dir(base)
    file_path = state_dir / safe_session_filename(session)
    if file_path.exists():
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "session_key": str(data.get("session_key", session)),
                    "window_ids": list(data.get("window_ids", [])),
                    "window_snapshots": list(data.get("window_snapshots", [])),
                    "evicted_buffer": list(data.get("evicted_buffer", [])),
                    "just_scrolled_out": list(data.get("just_scrolled_out", []))[:JUST_SCROLLED_MAX],
                    "rolling_summary": str(data.get("rolling_summary", ""))[:MAX_SUMMARY_CHARS],
                    "active_topics": list(data.get("active_topics", []))[:MAX_ACTIVE_TOPICS],
                    "active_speakers": list(data.get("active_speakers", [])),
                    "updated_at": float(data.get("updated_at", time.time())),
                }
        except (OSError, ValueError, TypeError):
            pass
    return {
        "session_key": session,
        "window_ids": [],
        "window_snapshots": [],
        "evicted_buffer": [],
        "just_scrolled_out": [],
        "rolling_summary": "",
        "active_topics": [],
        "active_speakers": [],
        "updated_at": time.time(),
    }


def save_rolling_state(base: Path | str, state: dict) -> None:
    """Atomically write rolling context state to disk."""
    state_dir = get_rolling_state_dir(base)
    session = state.get("session_key", "default")
    file_path = state_dir / safe_session_filename(session)
    state["updated_at"] = time.time()
    tmp_path = file_path.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, file_path)
    except OSError:
        pass


def is_commitment_signal(text: str) -> bool:
    raw = str(text or "")
    clean = strip_quote_and_markup(raw)
    return not is_noisy_eviction_candidate(raw, clean_text=clean) and bool(_COMMITMENT_RE.search(clean))


def is_preference_signal(text: str) -> bool:
    raw = str(text or "")
    clean = strip_quote_and_markup(raw)
    return not is_noisy_eviction_candidate(raw, clean_text=clean) and bool(_PREFERENCE_RE.search(clean))


def is_conclusion_signal(text: str) -> bool:
    raw = str(text or "")
    clean = strip_quote_and_markup(raw)
    return not is_noisy_eviction_candidate(raw, clean_text=clean) and bool(_CONCLUSION_RE.search(clean))


def extract_active_topics(texts: list[str], max_topics: int = MAX_ACTIVE_TOPICS) -> list[str]:
    """Extract up to max_topics salient keywords from dialogue texts."""
    all_terms: list[str] = []
    freq: dict[str, int] = {}
    for t in texts:
        if not t:
            continue
        try:
            t_set = terms(t, limit=800)
            for w in t_set:
                if len(w) >= 2:
                    freq[w] = freq.get(w, 0) + 1
                    if w not in all_terms:
                        all_terms.append(w)
        except Exception:
            pass
    # Sort terms by frequency descending, then recency
    sorted_terms = sorted(all_terms, key=lambda w: freq.get(w, 1), reverse=True)
    return sorted_terms[:max_topics]


def relative_label(at: float | int, *, now: float | None = None) -> str:
    """Generate intuitive relative time description (e.g. 刚才, 15分钟前, 今天 14:20, 昨天, 3天前)."""
    if not at:
        return ""
    now = time.time() if now is None else float(now)
    diff = max(0.0, now - float(at))
    if diff < 60:
        return "刚才"
    if diff < 3600:
        return f"{max(1, int(diff / 60))}分钟前"
    try:
        rec = datetime.fromtimestamp(at, CHAT_ZONE)
        ref = datetime.fromtimestamp(now, CHAT_ZONE)
        if (rec.year, rec.month, rec.day) == (ref.year, ref.month, ref.day):
            return f"今天 {rec.strftime('%H:%M')}"
        from datetime import timedelta
        yesterday = ref.date() - timedelta(days=1)
        if rec.date() == yesterday:
            return f"昨天 {rec.strftime('%H:%M')}"
        days = int(diff / 86400)
        if days < 7:
            return f"{days}天前"
        return rec.strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _synthesize_rolling_summary(evicted_buffer: list[dict], old_summary: str) -> str:
    """Deterministic extractive synthesis of rolling summary within 200~220 chars."""
    pieces: list[str] = []
    if old_summary and len(old_summary.strip()) > 0:
        prev = old_summary.strip()
        if len(prev) > 80:
            prev = prev[-80:]
        pieces.append(f"前情：{prev}")

    seen_pieces = set(pieces)
    for item in evicted_buffer:
        sender = item.get("sender_name") or item.get("user_id") or "某人"
        text = strip_quote_and_markup(str(item.get("text", "")))
        if not text:
            continue
        first_clause = re.split(r"[，。！？；\n]", text)[0].strip()[:35]
        if len(first_clause) >= 2:
            piece = f"{sender}提到了{first_clause}"
            if piece not in seen_pieces:
                seen_pieces.add(piece)
                pieces.append(piece)

    combined = "；".join(pieces)
    if len(combined) > MAX_SUMMARY_CHARS:
        combined = combined[-MAX_SUMMARY_CHARS:]
    return combined[:MAX_SUMMARY_CHARS]


def consolidate_evicted_buffer(
    evicted_buffer: list[dict],
    old_summary: str,
    store=None,
    session: str = "",
    active_topics: list[str] | None = None,
    now: float | None = None,
) -> tuple[str, list[dict], int]:
    """Persist high-value evicted messages to MemoryStore and refresh rolling summary."""
    now = time.time() if now is None else float(now)
    saved_count = 0
    active_topics = active_topics or []

    # 1. High value detection for uncommitted evicted messages
    for msg in evicted_buffer:
        if msg.get("saved"):
            continue
        raw_text = str(msg.get("text", "")).strip()
        user_id = str(msg.get("user_id", ""))
        sender = str(msg.get("sender_name") or user_id or "群友")
        clean = strip_quote_and_markup(raw_text)
        if is_noisy_eviction_candidate(raw_text, user_id=user_id, sender_name=sender, clean_text=clean):
            continue

        kind = None
        key = "日常经历"
        if is_commitment_signal(raw_text):
            kind = "commitment"
            key = "约定事项"
        elif is_preference_signal(raw_text):
            kind = "preference"
            key = "个人偏好"
        elif is_conclusion_signal(raw_text):
            kind = "episode"
            key = "讨论结论"

        if kind and store is not None:
            scoped_key = f"{key}·{user_id}" if user_id.isdigit() else key
            proposal = {
                "note": f"{sender}：{clean[:220]}",
                "kind": kind,
                "user_id": user_id if user_id.isdigit() else "",
                "key": scoped_key,
                "evidence": f"滑出窗口前原话：{clean[:120]}",
                "topics": active_topics[:3] if active_topics else ["群聊记忆"],
            }
            try:
                if hasattr(store, "remember_manual"):
                    receipt = store.remember_manual(
                        proposal,
                        session=session,
                        provider="rolling_context",
                        channel="rolling_eviction",
                        now=now,
                        origin_message_id=str(msg.get("message_id", "")),
                    )
                    if receipt.get("saved"):
                        saved_count += 1
                        msg["saved"] = True
                elif hasattr(store, "remember"):
                    store.remember(proposal, session=session, now=now)
                    saved_count += 1
                    msg["saved"] = True
            except Exception:
                pass

    # 2. Rolling summary consolidation when buffer accumulates threshold
    total_chars = sum(len(str(m.get("text", ""))) for m in evicted_buffer)
    new_summary = old_summary
    if len(evicted_buffer) >= EVICTION_MERGE_COUNT or total_chars >= EVICTION_MERGE_CHARS:
        new_summary = _synthesize_rolling_summary(evicted_buffer, old_summary)
        # Retain only the most recent 1-2 items in evicted buffer
        evicted_buffer = evicted_buffer[-2:]

    return new_summary, evicted_buffer, saved_count


def advance_rolling_window(
    base: Path | str,
    session: str,
    candidate_messages: list[dict],
    selected_messages: list[dict],
    target: dict,
    store=None,
    now: float | None = None,
    limit: int = STEADY_WINDOW_LIMIT,
) -> tuple[list[dict], dict]:
    """Smoothly advance the constant-water-level rolling context window.

    - Retains ~limit (default 12) messages in the active window.
    - Evicted messages are pushed to evicted_buffer and just_scrolled_out.
    - High-value items are automatically persisted to long-term memory.
    - Rolling relay summary is updated continuously.
    """
    now = time.time() if now is None else float(now)
    state = load_rolling_state(base, session)

    # Work with selected_messages or candidate_messages
    raw_incoming = list(selected_messages or candidate_messages or [])
    if not raw_incoming and candidate_messages:
        raw_incoming = list(candidate_messages)

    # Deduplicate while preserving order
    incoming: list[dict] = []
    seen_ids: set[str] = set()
    for m in raw_incoming:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("message_id", ""))
        if mid and mid in seen_ids:
            continue
        if mid:
            seen_ids.add(mid)
        incoming.append(m)

    # Determine steady messages window (keeping most recent `limit` messages)
    if len(incoming) > limit:
        steady_messages = incoming[-limit:]
        newly_evicted_raw = incoming[:-limit]
    else:
        steady_messages = incoming
        newly_evicted_raw = []

    # Map previous window snapshots
    prev_snapshots = state.get("window_snapshots", [])
    current_mids = {str(m.get("message_id", "")) for m in steady_messages if m.get("message_id")}

    # Any message in previous snapshots not present in current steady messages is evicted
    evicted_from_prev = [
        snap for snap in prev_snapshots
        if snap.get("message_id") and snap["message_id"] not in current_mids
    ]

    # Convert newly evicted raw messages to snapshots
    newly_evicted_snaps: list[dict] = []
    for m in newly_evicted_raw:
        mid = str(m.get("message_id", ""))
        newly_evicted_snaps.append({
            "message_id": mid,
            "user_id": str(m.get("user_id", "")),
            "sender_name": str(m.get("nickname") or m.get("sender_name") or m.get("user_id", "群友")),
            "text": str(m.get("text", ""))[:300],
            "timestamp": float(m.get("received_at") or m.get("time") or m.get("at") or now),
            "saved": False,
        })

    # Merge newly evicted into evicted_buffer avoiding duplicates
    existing_evicted_ids = {str(m.get("message_id")) for m in state["evicted_buffer"] if m.get("message_id")}
    for snap in evicted_from_prev + newly_evicted_snaps:
        mid = str(snap.get("message_id", ""))
        if mid and mid not in existing_evicted_ids and mid not in current_mids:
            existing_evicted_ids.add(mid)
            state["evicted_buffer"].append(snap)

    # Update just_scrolled_out: take last up to 3 evicted items, format compact line <= 60 chars
    all_evicted_recent = (evicted_from_prev + newly_evicted_snaps)[-JUST_SCROLLED_MAX:]
    if all_evicted_recent:
        scrolled_lines = []
        for snap in all_evicted_recent:
            sender = snap.get("sender_name") or snap.get("user_id") or "群友"
            txt = (strip_quote_and_markup(str(snap.get("text", ""))) or str(snap.get("text", "")).strip().replace("\n", " "))[:JUST_SCROLLED_CHAR_LIMIT - 12]
            scrolled_lines.append(f"[刚滑出] {sender}: {txt}")
        state["just_scrolled_out"] = scrolled_lines

    # Extract active topics from steady messages and current rolling summary
    active_texts = [str(m.get("text", "")) for m in steady_messages]
    if state["rolling_summary"]:
        active_texts.append(state["rolling_summary"])
    if target and target.get("text"):
        active_texts.append(str(target["text"]))
    state["active_topics"] = extract_active_topics(active_texts, max_topics=MAX_ACTIVE_TOPICS)

    # Extract active speakers
    speakers: list[str] = []
    for m in steady_messages:
        uid = str(m.get("user_id", ""))
        if uid and uid != "SELF" and uid not in speakers:
            speakers.append(uid)
    t_uid = str(target.get("user_id", "")) if target else ""
    if t_uid and t_uid != "SELF" and t_uid not in speakers:
        speakers.append(t_uid)
    state["active_speakers"] = speakers

    # Consolidate evicted buffer (durable save + rolling summary)
    new_summary, remaining_buffer, _saved = consolidate_evicted_buffer(
        state["evicted_buffer"],
        state["rolling_summary"],
        store=store,
        session=session,
        active_topics=state["active_topics"],
        now=now,
    )
    state["rolling_summary"] = new_summary
    state["evicted_buffer"] = remaining_buffer

    # Update window snapshots and window_ids for next round
    state["window_ids"] = [str(m.get("message_id", "")) for m in steady_messages if m.get("message_id")]
    state["window_snapshots"] = [
        {
            "message_id": str(m.get("message_id", "")),
            "user_id": str(m.get("user_id", "")),
            "sender_name": str(m.get("nickname") or m.get("sender_name") or m.get("user_id", "")),
            "text": str(m.get("text", ""))[:300],
            "timestamp": float(m.get("received_at") or m.get("time") or m.get("at") or now),
        }
        for m in steady_messages
    ]

    save_rolling_state(base, state)
    return steady_messages, state


def timeline_row(line: str) -> dict:
    """The horizontal_timeline row derived from one scoped note (pure)."""
    # Extract timestamp or note label for timeline
    m_match = re.search(r"\[M(\d+)\]\s*([^·]+)·(?:记)?([0-9\-\s:]+)：(.*)", line)
    if m_match:
        mid, label, timestr, excerpt = m_match.groups()
        return {
            "id": f"M{mid}",
            "label": label.strip(),
            "time": timestr.strip(),
            "excerpt": excerpt.strip()[:100],
        }
    return {
        "id": line[:15],
        "label": "记忆",
        "time": "近期",
        "excerpt": line[:100],
    }


def build_memory_axes(
    notes: list[str],
    rolling_bridge: dict,
    messages: list[dict],
    target: dict,
    now: float | None = None,
) -> dict:
    """Organize memories and rolling state into a structured dual-axis view.

    - rolling_bridge: summary, recently evicted snippets, active topic anchors
    - vertical_threads: portraits, commitments, current topic & event depth
    - horizontal_timeline: chronological timeline with relative anchors
    """
    now = time.time() if now is None else float(now)
    rolling_summary = str(rolling_bridge.get("rolling_summary", "") if isinstance(rolling_bridge, dict) else "")
    just_scrolled = list(rolling_bridge.get("just_scrolled_out", []) if isinstance(rolling_bridge, dict) else [])
    active_topics = list(rolling_bridge.get("active_topics", []) if isinstance(rolling_bridge, dict) else [])

    vertical_person: list[str] = []
    vertical_topics: list[str] = []
    timeline_items: list[dict] = []

    for note in notes:
        line = str(note)
        if any(tag in line for tag in ("人物", "约定", "偏好", "称呼")):
            vertical_person.append(line)
        else:
            vertical_topics.append(line)

        timeline_items.append(timeline_row(line))

    return {
        "rolling_bridge": {
            "rolling_summary": rolling_summary,
            "just_scrolled_out": just_scrolled,
            "active_topics": active_topics,
        },
        "vertical_threads": {
            "person_and_commitments": vertical_person,
            "topics_and_episodes": vertical_topics,
        },
        "horizontal_timeline": timeline_items,
    }
