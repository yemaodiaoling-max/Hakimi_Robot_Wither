"""Small, provenance-preserving personal memory retrieval; no extra model calls.

Manual notes are validated here but stored by MemoryStore.  The exact note text is
never stripped, truncated or rewritten; only its emptiness is checked.  Both model
backends use this same validation/write path (Gemini calls memory.remember, dsh
supplies the JSON "memory" field; neither becomes a native tool call).
"""
import json
import math
from pathlib import Path
import re
import time
from hakimi.memory.text import stamp, terms, valid_source  # noqa: F401 (re-export)

MAX_MANUAL_CHARS = None  # Legacy import compatibility; manual originals have no length cap.
MANUAL_KINDS = {"person", "preference", "lore", "episode", "reflection", "commitment"}
ALLOWED_FIELDS = {"kind", "note", "key", "source", "evidence", "user_id", "topics",
                  "related_memory_ids"}


def related(note, query):
    return len(terms(note) & terms(query)) >= 2


def _valid_list(value, *, limit, max_length, numeric=False):
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，\n]", value)
    elif isinstance(value, (list, tuple)):
        parts = value
    else:
        return None
    result = []
    for part in list(parts)[:limit]:
        text = str(part).strip()
        if numeric:
            text = text.upper().removeprefix("M")
            if not text.isdigit() or not 1 <= len(text) <= 20:
                return None
            result.append("M" + text)
        else:
            if not 1 <= len(text) <= max_length or any(ord(ch) < 32 for ch in text):
                return None
            result.append(text)
    if len(parts) > limit:
        return None
    return list(dict.fromkeys(result))


def valid_memory(proposal):
    """Validate a model-proposed note without changing a single character of it."""
    if not isinstance(proposal, dict) or set(proposal) - ALLOWED_FIELDS:
        return None
    kind = proposal.get("kind", "person")
    if kind not in MANUAL_KINDS:
        return None
    note = proposal.get("note")
    if not isinstance(note, str) or not note.strip():
        return None
    key = proposal.get("key", "")
    if not isinstance(key, str) or len(key) > 64:
        return None
    evidence = proposal.get("evidence", "")
    if not isinstance(evidence, str) or len(evidence) > 240:
        return None
    source = proposal.get("source", "")
    if not isinstance(source, str) or len(source) > 400:
        return None
    if kind == "lore":
        if not valid_source(source):
            return None
    elif source:
        return None
    topics = _valid_list(proposal.get("topics"), limit=8, max_length=40)
    related_ids = _valid_list(proposal.get("related_memory_ids"), limit=8, max_length=20, numeric=True)
    if topics is None or related_ids is None:
        return None
    user_id = str(proposal.get("user_id", "") or "")
    if kind == "person":
        if not re.fullmatch(r"[1-9][0-9]{0,19}", user_id):
            return None
    elif user_id:
        return None
    result = {"kind": kind, "note": note, "key": key.strip(), "source": source}
    if evidence:
        result["evidence"] = evidence
    if kind == "person":
        result["user_id"] = user_id
    if topics:
        result["topics"] = topics
    if related_ids:
        result["related_memory_ids"] = related_ids
    return result


def valid_person(proposal):
    if isinstance(proposal, dict):
        proposal = {**proposal, "kind": "person"}
    value = valid_memory(proposal)
    if not value:
        return None
    return {"user_id": value["user_id"], "note": value["note"], "key": value["key"],
            "evidence": value.get("evidence", ""), "topics": value.get("topics", []),
            "related_memory_ids": value.get("related_memory_ids", [])}


def _append_jsonl(path, entry):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.exists():
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 131072))
            tail = stream.read()
        prefix = "" if not tail or tail.endswith(b"\n") else "\n"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(prefix + json.dumps(entry, ensure_ascii=False) + "\n")


def remember_memory(base, session, proposal, users, *, provider="gemini", now=None,
                    origin_message_id="", channel=""):
    """Validated, provider-neutral manual write.  Returns an explicit receipt dict."""
    proposal = valid_memory(proposal)
    if not proposal:
        return {"saved": False, "error": "invalid_note"}
    if proposal["kind"] == "person" and str(proposal["user_id"]) not in {str(key) for key in users}:
        return {"saved": False, "error": "person_not_visible"}
    from hakimi.memory.memory_store import store
    index = store(str(Path(base).resolve()))
    related = proposal.get("related_memory_ids") or []
    if related:
        # A group note may deliberately link a note from another group (proactive
        # cross-group recall), but a private session never gains group visibility.
        cross = str(session).startswith("group:")
        visible = index.visible_record_ids(session, users, related, cross_groups=cross)
        if len(visible) != len(set(related)):
            return {"saved": False, "error": "related_memory_unreadable"}
    result = index.remember_manual(
        proposal, session=session, provider=provider, channel=channel or provider,
        now=time.time() if now is None else now, origin_message_id=origin_message_id,
    )
    if result.get("saved") and result.get("created") and result.get("entry") is not None:
        try:
            _append_jsonl(Path(base) / "state/session_memory.jsonl", result["entry"])
            result["jsonl_status"] = "appended"
        except OSError:
            # The SQLite original/index already exist and can rebuild.  A JSONL
            # failure must not make the caller retry and create a duplicate.
            result["jsonl_status"] = "error"
    elif result.get("saved"):
        result["jsonl_status"] = "existing"
    result.pop("entry", None)
    result.pop("record_text", None)
    return result


def save_memory(base, session, proposal, users, *, provider="gemini", now=None, origin_message_id=""):
    """Backward-compatible boolean wrapper used by fallback and old tests."""
    result = remember_memory(base, session, proposal, users, provider=provider, now=now,
                             origin_message_id=origin_message_id, channel=provider)
    return bool(result.get("saved") and result.get("created"))


def visible_users(payload):
    from hakimi.core.context_window import select_context
    session = payload.get("session_key")
    return {str(row.get("user_id")): {"message_id": str(row.get("message_id", ""))[:30]}
            for row in select_context(payload)[0] if isinstance(row, dict)
            and row.get("session_key") == session and re.fullmatch(r"[1-9][0-9]{0,19}", str(row.get("user_id", "")))}


def save_person(base, session, proposal, users, *, provider="gemini", now=None):
    proposal = valid_person(proposal)
    if not proposal or proposal["user_id"] not in {str(key) for key in users}:
        return False
    return save_memory(base, session, proposal, users, provider=provider, now=now)


def alive(row, now, days=30):
    at, until = row.get("created_at", row.get("received_at", 0)), row.get("expires_at", now+1)
    return (type(at) in (int, float) and type(until) in (int, float) and math.isfinite(at)
            and math.isfinite(until) and 0 <= now-at <= days*86400 and now < until)


def shared_records(session, messages, target, memories, history, now, lore=(), budget=300):
    """Same official IDs only; topic evidence is quoted, never promoted to fact."""
    users = {str(row.get("user_id")) for row in messages
             if re.fullmatch(r"[1-9][0-9]{0,19}", str(row.get("user_id", "")))}
    focus = str(target.get("user_id", ""))
    query = str(target.get("text", "")) or " ".join(row.get("text", "") for row in messages[-2:])
    candidates, seen = [], set()
    for row in sorted(memories, key=lambda r:r.get("created_at", 0) if type(r.get("created_at")) in (int,float) else 0, reverse=True):
        person = str(row.get("person_id", ""))
        key = (person, row.get("key", ""))
        if row.get("kind") != "person" or person not in users or key in seen:
            continue
        seen.add(key)
        if not alive(row, now, 180):
            continue
        note = str(row.get("note", ""))[:150]
        candidates.append((10+(person == focus), row["created_at"],
            f"人物{person}·{row.get('key','偏好')}（{stamp(row['created_at'])}，来源{row.get('session_key')}）：{note}"))
    recall = bool(re.search(r"认识我|认得我|我是谁|记得我|还记得|见过我|之前聊", query))
    for row in history:
        person = str(row.get("user_id", ""))
        if (person not in users or row.get("session_key") == session
                or not str(row.get("session_key", "")).startswith("group:") or not alive(row, now)):
            continue
        text = re.sub(r"\[CQ:[^\]]*\]|https?://\S+|@哈基米[^\s]*", " ", str(row.get("text", "")))
        text = re.sub(r"\[(?:QQ表情|表情包图片|群友发图)[^\]]*\]", " ", text)
        if len(text.strip()) < 6 or not terms(text):
            continue
        preference = bool(re.search(r"叫我|称呼我|我喜欢|我不喜欢|我在学|我研究", text))
        if not (related(text, query) or preference or recall and person == focus):
            continue
        at = row.get("received_at", row.get("created_at", 0))
        candidates.append((2+2*preference+(person == focus), at,
            f"同一账号{person}的旧记录（{stamp(at)}，来源{row.get('session_key')}，曾用昵称{str(row.get('nickname',''))[:20]}，原话不等于事实）：{text.strip()[:90]}"))
    candidates += [(4, row["created_at"], text) for row, text in lore if related(row.get("note", ""), query)]
    result, texts, episodes = [], set(), set()
    for priority, at, text in sorted(candidates, key=lambda item:(item[0],item[1]), reverse=True):
        if text in texts or len(text) > budget:
            continue
        match = re.match(r"同一账号([0-9]+)", text)
        if match and match[1] in episodes:
            continue
        if match:episodes.add(match[1])
        result.append(text); texts.add(text); budget -= len(text)
        if len(result) == 3:
            break
    return result
