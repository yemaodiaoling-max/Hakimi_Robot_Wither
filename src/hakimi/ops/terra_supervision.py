"""Fixed two-hour Terra supervision: style guidance and member relationships.

One bounded, read-only Codex CLI call (gpt-6.1-terra) may combine:

* delivered DSH style samples from the last two hours (all, or a stratified 12);
* up to eight current Gemini samples for voice comparison;
* at most five changed member-to-member directional relationships.

The host validates the strict JSON, writes only allowlisted state files, and
stores relationship notes through the existing exact-original memory_social API
with provider=terra and provenance=terra_assisted.  It never sends QQ output,
never grants tools/permissions, and never rewrites Gemini persona or facts.
"""
from __future__ import annotations
from hakimi import paths
from hakimi import persona

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from hakimi.features.codex_help_runner import CodexHelpError, run_codex_structured
from hakimi.ops.terra_budget import BATCH_RESERVE, BudgetExhausted

TERRA_MODEL = "gpt-6.1-terra"
CYCLE_SECONDS = 2 * 3600
SAMPLE_WINDOW = 2 * 3600
MAX_DSH = 12
MAX_GEMINI = 8
MAX_RELATIONS = 5
MAX_EVIDENCE = 6
MIN_RELATION_SLOTS = 1
MAX_PROMPT = 14000
FEEDBACK_LIMIT = 500
UNSAFE_DIAGNOSIS = re.compile(r"确诊|诊断|精神病|性取向|性关系|怀孕|恋爱关系|爱上你")
PERSON = re.compile(r"[1-9][0-9]{0,19}")

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "style": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "verdict": {"type": "string", "enum": ["normal", "problem", "insufficient"]},
                "guidance": {"type": "string", "maxLength": FEEDBACK_LIMIT},
                "uncertainty": {"type": "string", "maxLength": 300},
            },
            "required": ["verdict", "guidance", "uncertainty"],
        },
        "relations": {
            "type": "array", "maxItems": MAX_RELATIONS,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "subject_id": {"type": "string", "maxLength": 20},
                    "target_id": {"type": "string", "maxLength": 20},
                    "relationship_type": {"type": "string", "minLength": 1, "maxLength": 40},
                    "note": {"type": "string", "minLength": 1, "maxLength": 240},
                    "reason": {"type": "string", "maxLength": 160},
                    "affinity": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
                    "closeness": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
                    "confidence": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
                    "evidence_memory_ids": {
                        "type": "array", "minItems": 1, "maxItems": 8,
                        "items": {"type": "string", "maxLength": 24},
                    },
                },
                "required": ["subject_id", "target_id", "relationship_type", "note",
                             "reason", "evidence_memory_ids", "affinity", "closeness", "confidence"],
            },
        },
    },
    "required": ["style", "relations"],
}


def _state_path(base):
    return Path(base) / "state" / "terra_watch" / "terra_cycle.json"


def _feedback_path(base):
    base = Path(base)
    root = base.parent if base.name == "official_test_runtime" else base
    return root / "state" / "dsh_style_feedback.md"


def _load(base):
    try:
        value = json.loads(_state_path(base).read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("version") == 1:
            value.setdefault("reviewed_signatures", [])
            value.setdefault("counts", {})
            value.setdefault("direction_cursors", {})
            return value
    except (OSError, ValueError):
        pass
    return {"version": 1, "next_due": 0, "reviewed_signatures": [], "counts": {},
            "direction_cursors": {}, "last_relation_signature": "", "last_feedback_at": 0}


def _save(base, state):
    path = _state_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["reviewed_signatures"] = [str(item) for item in state.get("reviewed_signatures", [])][-48:]
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > 256 * 1024:
        raise RuntimeError("terra checkpoint too large")
    temp = path.with_suffix(".tmp")
    temp.write_text(raw, encoding="utf-8")
    temp.replace(path)


def _tail_rows(path, limit=524288):
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            start = max(0, stream.tell() - limit)
            stream.seek(start)
            if start:
                stream.readline()
            raw = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except ValueError:
            pass
    return rows


def _day(at):
    try:
        return datetime.fromtimestamp(float(at), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _stratified(rows, limit):
    """All small batches; otherwise fair group quotas and time quantiles."""
    rows = sorted(rows, key=lambda row:(float(row.get('at',0) or 0),str(row.get('message_id',''))))
    if len(rows)<=limit:
        return rows
    groups={}
    for row in rows:
        groups.setdefault(str(row.get('session_key','')),[]).append(row)
    keys=sorted(groups,key=lambda key:(-len(groups[key]),key))
    quota={key:0 for key in keys}
    remaining=limit
    while remaining:
        advanced=False
        for key in keys:
            if not remaining:break
            if quota[key]<len(groups[key]):
                quota[key]+=1;remaining-=1;advanced=True
        if not advanced:break
    picked=[]
    for key in keys:
        values=groups[key];n=quota[key]
        indexes=[len(values)-1] if n==1 else [round(i*(len(values)-1)/(n-1)) for i in range(n)] if n else []
        picked.extend(values[i] for i in indexes)
    return sorted(picked,key=lambda row:float(row.get('at',0) or 0))[:limit]


def collect_dsh_samples(base, now, *, limit=MAX_DSH, window_seconds=SAMPLE_WINDOW):
    rows = [row for row in _tail_rows(Path(base) / "state" / "sent_messages.jsonl")
            if row.get("provider") == "dsh" and isinstance(row.get("text"), str)
            and row["text"].strip() and isinstance(row.get("session_key"), str)
            and row.get("message_id") and isinstance(row.get("at"), (int, float))
            and row["session_key"].startswith("group:")
            and 0 <= now - float(row["at"]) <= window_seconds]
    unique = {}
    for row in rows:
        unique[(str(row["session_key"]),str(row["message_id"]))] = row
    selected = _stratified(list(unique.values()), limit)
    return [{"id": str(row["message_id"]), "session_key": str(row["session_key"]),
             "at": float(row["at"]), "date": _day(row["at"]),
             "text": str(row["text"])[:700]} for row in selected]


def _profile_for(base, session):
    try:
        cfg = json.loads((Path(base) / "group_profiles.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not str(session).startswith("group:"):
        return str(cfg.get("default_profile", ""))
    return str((cfg.get("groups", {}).get(str(session)[6:], {}) or {}).get(
        "profile", cfg.get("default_profile", "")))


def _baseline_rows(base, dsh):
    wanted_sessions = {row["session_key"] for row in dsh}
    wanted_profiles = {_profile_for(base, session) for session in wanted_sessions if _profile_for(base, session)}
    root=Path(base).resolve();root=root.parent if root.name=="official_test_runtime" else root
    frozen = root / "state/dsh_gemini38_style.jsonl"
    path = frozen if frozen.is_file() else paths.persona("dsh_style_examples.jsonl", base)
    rows = []
    for row in _tail_rows(path):
        if (row.get("provider") != "gemini" or row.get("provenance") != "recorded_gemini_output" or row.get("model") != "gemini-3.8-flash"
                or not str(row.get('session_key','')).startswith('group:')
                or not isinstance(row.get("source"), dict)):
            continue
        parts = row.get("text_segments")
        text = " ".join(parts) if isinstance(parts, list) and parts and all(isinstance(p, str) for p in parts) else row.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        rows.append({"id": str(row.get("message_id") or row.get("id") or ""),
                     "session_key": str(row.get("session_key", "")), "text": text[:700],
                     "source": "同群历史确认基准" if str(row.get("session_key", "")) in wanted_sessions
                               else "同风格历史确认基准，仅比较口吻",
                     "age_days": max(0.0, now_age(row.get("at"))) if now_age(row.get("at")) is not None else None})
    same = [row for row in rows if row["session_key"] in wanted_sessions]
    if same:
        return same[-MAX_GEMINI:]
    matching = [row for row in rows if _profile_for(base, row["session_key"]) in wanted_profiles]
    return (matching or [])[-MAX_GEMINI:]


def now_age(at):
    try:
        return (time.time() - float(at)) / 86400
    except (TypeError, ValueError):
        return None


def collect_gemini_samples(base, now, dsh):
    baseline = _baseline_rows(base, dsh)
    if not baseline:
        return []
    chosen = _stratified([{**row, "at": 0} for row in baseline], MAX_GEMINI)
    for row in chosen:
        row["at"] = 0
    return chosen


def _self_ids(base):
    result = set()
    try:
        cfg = json.loads((Path(base) / "config.json").read_text(encoding="utf-8"))
        value = str((cfg.get("bot_profile") or {}).get("self_qq", ""))
        if PERSON.fullmatch(value):
            result.add(value)
    except (OSError, ValueError, AttributeError):
        pass
    return result


def _targets(value):
    if isinstance(value, (list, tuple)):
        return {str(item) for item in value if PERSON.fullmatch(str(item))}
    if not isinstance(value, str) or not value.strip():
        return set()
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return {str(item) for item in parsed if PERSON.fullmatch(str(item))}
    except (TypeError, ValueError):
        pass
    return {item for item in re.findall(r"[1-9][0-9]{0,19}", value)}


def _confidence_cap(support, days):
    if support < 2:
        return 0
    if support >= 3 and len(days) >= 2:
        return 85
    if support >= 3:
        return 70
    return 55


def _prior_direction(db, subject, target):
    row = db.execute(
        "SELECT id,impression,affinity,closeness,confidence,evidence_record_ids,"
        "created_at,session,affinity_policy FROM social_assessments "
        "WHERE subject_id=? AND target_id=? AND provenance='terra_assisted' "
        "AND state='active' AND session LIKE 'group:%' "
        "ORDER BY created_at DESC,id DESC LIMIT 1", (str(subject), str(target)),
    ).fetchone()
    if not row:
        return None
    try:
        evidence = [int(value) for value in json.loads(row["evidence_record_ids"] or "[]")][:MAX_EVIDENCE]
    except (TypeError, ValueError):
        evidence = []
    try:
        policy = json.loads(row["affinity_policy"] or "{}")
    except (TypeError, ValueError):
        policy = {}
    return {
        "assessment_id": int(row["id"]),
        "impression": str(row["impression"] or "")[:600],
        "affinity": row["affinity"], "closeness": row["closeness"],
        "confidence": row["confidence"],
        "evidence_memory_ids": ["M" + str(value) for value in evidence],
        "created_at": float(row["created_at"] or 0),
        "session": str(row["session"] or ""),
        "affinity_policy": policy if isinstance(policy, dict) else {},
    }


def _direction_evidence(db, subject, target, item, now):
    ids = [int(value) for value in item.get("evidence", [])][:20]
    rows = []
    if ids:
        marks = ",".join("?" for _ in ids)
        rows = db.execute(
            "SELECT id,user_id,session,at,text,direct_targets,reply_to FROM records "
            "WHERE kind='chat' AND state!='deleted' AND id IN (" + marks + ") "
            "ORDER BY at DESC,id DESC", ids).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    own = []
    for value in ids:
        row = by_id.get(value)
        if not row or str(row["user_id"]) != str(subject):
            continue
        text = str(row["text"] or "").strip()
        if not text:
            continue
        own.append({"memory_id": "M" + str(row["id"]), "user_id": str(row["user_id"]),
                    "at": float(row["at"] or 0), "date": _day(row["at"]),
                    "session": str(row["session"]), "excerpt": text[:200]})
        if len(own) >= MAX_EVIDENCE:
            break
    return own


def relation_candidates(base, now, *, limit=MAX_RELATIONS, state=None):
    """Directional, current public evidence; no 2-day/3-event writing gate."""
    from hakimi.social.interaction_familiarity import interaction_stats
    from hakimi.memory.memory_store import store as store_factory
    state = _load(base) if state is None else state
    cursors = state.get("direction_cursors") if isinstance(state.get("direction_cursors"), dict) else {}
    index = store_factory(str(Path(base).resolve()))
    with index.db() as db:
        stats = interaction_stats(db, session=None, self_ids=_self_ids(base), limit=50000)
        ranked = sorted(
            ((key, item) for key, item in stats.items()
             if "hakimi" not in key and int(item.get("events", 0)) >= 1
             and float(item.get("last_at", 0) or 0) >= now - 30 * 86400),
            key=lambda pair: (float(pair[1].get("last_at", 0)), int(pair[1].get("events", 0)),
                              pair[0]), reverse=True,
        )
        result = []
        for key, item in ranked:
            for subject, target in ((key[0], key[1]), (key[1], key[0])):
                events = int((item.get("direction_events") or {}).get(subject, 0) or 0)
                if events <= 0:
                    continue
                own = _direction_evidence(db, subject, target, item, now)
                if not own:
                    continue
                days = sorted((item.get("directions") or {}).get(subject) or [])
                support = max(events, len(own))
                numeric = support >= 2 and len(own) >= 2
                cap = _confidence_cap(support, days)
                prior = _prior_direction(db, subject, target)
                cursor = cursors.get(str(subject) + "->" + str(target))
                cursor = cursor if isinstance(cursor, dict) else {}
                current_ids = sorted(int(row["memory_id"].removeprefix("M")) for row in own)
                last_ids = set()
                for value in cursor.get("last_evidence_ids", []) or (
                        prior or {}).get("evidence_memory_ids", []):
                    text = str(value).upper().removeprefix("M")
                    if text.isdigit():
                        last_ids.add(int(text))
                seen_max = max(int(cursor.get('max_evidence_id',0) or 0), max(last_ids, default=0))
                has_new = not seen_max or any(value > seen_max for value in current_ids)
                if not has_new:
                    continue
                from hakimi.memory.memory_lifecycle import impression_novelty
                novelty = impression_novelty((prior or {}).get("impression", ""),
                                             [row["excerpt"] for row in own])
                last_run = float(cursor.get("last_run_at", 0) or 0)
                never = 1 if not last_run else 0
                neglect = min(3.0, max(0.0, now - last_run) / 7200) if last_run else 3.0
                priority = (3.2 * never + 2.0 + 1.5 * (0 if prior else 1)
                            + 1.2 * float(novelty) + neglect
                            + math.log1p(events) * 0.3)
                fingerprint = hashlib.sha256(json.dumps(
                    [str(subject), str(target), current_ids,
                     (prior or {}).get("assessment_id")],
                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
                result.append({
                    "subject_id": str(subject), "target_id": str(target),
                    "direction": str(subject) + "->" + str(target),
                    "session": own[0]["session"], "events": events,
                    "days": days, "support": support,
                    "numeric_allowed": numeric,
                    "confidence_cap": cap,
                    "confidence_tier": ("multi_day" if cap == 85 else
                                        "same_day" if cap == 70 else
                                        "sparse" if cap == 55 else "descriptive"),
                    "evidence": own,
                    "prior_assessment": prior,
                    "novelty": round(float(novelty), 3),
                    "has_new_evidence": True,
                    "fingerprint": fingerprint,
                    "last_run_at": last_run,
                    "priority": round(priority, 4),
                    "context_note": "仅来自公开群真实@/引用；同场聊天只作语境，不当作关系事实",
                })
        result.sort(key=lambda item: (item["priority"], item["events"], item["direction"]),
                    reverse=True)
        return result[:max(1, int(limit))]


def _prompt(dsh, gemini, relations, trial=None, bots=()):
    policy=persona.text("ops/terra_supervisor.txt")
    policy += ("\n关系仅处理本轮新增/变更候选。subject→target与反向分别判断，"
               "不把哈基米自己的好感移用为群友之间的态度。note写简短当前倾向，"
               "reason一句话连接证据；旧结论仅作背景，证据不足留null，不重复旧证据推分。\n")
    if bots:policy+=persona.text("ops/terra_bot_reviews.txt")
    room = MAX_PROMPT - len(policy) - 120
    selected_dsh = dsh[:MAX_DSH]
    selected_gemini = gemini[:MAX_GEMINI]
    relation_limit = min(len(relations), MAX_RELATIONS)

    def build(dsh_cap, gemini_cap, rel_count, evidence_count):
        def excerpt(row, cap):
            item = dict(row)
            text = str(item.get('text', ''))
            item['text'] = text[:cap]
            item['excerpt_only'] = len(text) > cap or bool(item.get('excerpt_only'))
            return item
        rels = []
        for relation in relations[:rel_count]:
            item = {key: relation[key] for key in (
                'subject_id', 'target_id', 'direction', 'session', 'fingerprint',
                'numeric_allowed', 'confidence_cap') if key in relation}
            item['evidence'] = [dict(row, excerpt=str(row.get('excerpt', ''))[:max(120, dsh_cap)])
                                for row in relation.get('evidence', [])[:evidence_count]]
            prior = relation.get('prior_assessment')
            if isinstance(prior, dict):
                item['prior_assessment'] = {key: prior.get(key) for key in (
                    'assessment_id', 'affinity', 'closeness', 'confidence', 'evidence_memory_ids')}
                item['prior_assessment']['impression'] = str(prior.get('impression', ''))[:240]
            rels.append(item)
        payload = {
            'dsh_samples': [excerpt(row, dsh_cap) for row in selected_dsh],
            'gemini_comparison': [excerpt(row, gemini_cap) for row in selected_gemini],
            'relationship_candidates': rels,
            'model_trial': trial,
            'schema_note': '所有选中dsh样本逐条保留；长文仅为风格片段。仅评估候选方向；分数未知填null。',
        }
        if bots:payload['bot_candidates']=list(bots)
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))

    relation_limit = max(MIN_RELATION_SLOTS, relation_limit) if relations else 0
    evidence_count = MAX_EVIDENCE
    gemini_cap = 700
    # Keep every selected dsh row and at least one relation slot; shrink text
    # first, then relation count down to the minimum, then evidence breadth.
    for _phase in range(6):
        for dsh_cap in (700, 500, 350, 220, 160, 120, 80, 40, 1):
            raw = build(dsh_cap, gemini_cap, relation_limit, evidence_count)
            if len(raw) <= room:
                return policy + raw
        if relation_limit > MIN_RELATION_SLOTS:
            relation_limit = max(MIN_RELATION_SLOTS, relation_limit - 1)
            continue
        if evidence_count > 1:
            evidence_count = 1
            continue
        if gemini_cap > 120:
            gemini_cap = 120
            continue
        break
    raw = build(0, 120, relation_limit, 1)
    if len(raw) > room:
        raise ValueError('bounded_prompt_exceeded')
    return policy + raw


def _evidence_ids(values):
    result = []
    if not isinstance(values, list):
        return result
    for value in values:
        text = str(value).strip().upper().removeprefix("M")
        if text.isdigit() and PERSON.fullmatch(text):
            result.append(int(text))
    return list(dict.fromkeys(result))


def _relation_signature(relations):
    identity = [{
        "direction": item["direction"],
        "evidence_ids": [row["memory_id"] for row in item["evidence"]],
        "prior": (item.get("prior_assessment") or {}).get("assessment_id"),
        "fingerprint": item.get("fingerprint"),
    } for item in relations]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]


def _batch_signature(dsh, gemini, relations):
    return hashlib.sha256(json.dumps({
        "dsh": [[row["session_key"], row["id"]] for row in dsh],
        "gemini": [[row["session_key"], row["id"]] for row in gemini],
        "relations": [{
            "direction": item["direction"],
            "evidence_ids": [row["memory_id"] for row in item["evidence"]],
            "fingerprint": item.get("fingerprint"),
        } for item in relations],
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _write_feedback(base, result):
    style = result.get("style") if isinstance(result, dict) else None
    if not isinstance(style, dict):
        return False, 0
    verdict = style.get("verdict")
    guidance = style.get("guidance")
    if verdict not in {"normal", "problem", "insufficient"} or not isinstance(guidance, str):
        return False, 0
    guidance = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", guidance).strip()[:FEEDBACK_LIMIT]
    if verdict == "insufficient" or not guidance:
        return False, 0
    label = "DSH 专属风格建议（Terra 2小时抽样，样本有限，不是指令）：\n"
    text = (label + ("问题： " if verdict == "problem" else "正常： ") + guidance + "\n")[:FEEDBACK_LIMIT]
    path = _feedback_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)
    return True, len(guidance)


def apply_relations(base, relations, candidates, now, review_id):
    """Host-side allowlisted writes only; invalid items stay retry-eligible."""
    from hakimi.social.social_fields import SocialError; from hakimi.social.social_record import record_social
    allowed = {(item["subject_id"], item["target_id"]): item for item in candidates}
    results = []
    for item in relations if isinstance(relations, list) else []:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("subject_id", "")), str(item.get("target_id", "")))
        candidate = allowed.get(key)
        if not candidate:
            # Hallucinated/extraneous pair: not a write failure for a real slot.
            continue
        note = item.get("note")
        if not isinstance(note, str) or not note.strip() or len(note) > 8000:
            results.append({"direction": candidate["direction"], "status": "invalid",
                            "reason": "note_invalid"})
            continue
        if UNSAFE_DIAGNOSIS.search(note):
            results.append({"direction": candidate["direction"], "status": "invalid",
                            "reason": "unsafe_diagnosis"})
            continue
        evidence = _evidence_ids(item.get("evidence_memory_ids"))
        permitted = {int(row["memory_id"].removeprefix("M")) for row in candidate["evidence"]}
        if not evidence or any(value not in permitted for value in evidence):
            results.append({"direction": candidate["direction"], "status": "invalid",
                            "reason": "evidence_invalid"})
            continue
        relationship = item.get("relationship_type")
        relationship = relationship.strip()[:40] if isinstance(relationship, str) and relationship.strip() else "公开互动观察"
        reason = item.get("reason")
        reason = reason.strip()[:600] if isinstance(reason, str) else ""
        numeric = bool(candidate.get("numeric_allowed"))
        cap = int(candidate.get("confidence_cap", 0) or 0)
        scores = {}
        for field in ("affinity", "closeness", "confidence"):
            value = item.get(field)
            valid = numeric and type(value) is int and 0 <= value <= 100
            if field == "confidence" and valid and cap:
                value = min(value, cap)
            scores[field] = value if valid else None
        if not numeric:
            reason = (reason + "；直接互动证据不足2条，只作描述，分数未知").strip("；")[:600]
        elif cap and scores["confidence"] is not None and scores["confidence"] >= cap:
            reason = (reason + f"；稀疏/单日证据置信度封顶{cap}").strip("；")[:600]
        # Host adds only the same directional evidence actually presented to
        # Terra. This makes support auditable instead of trusting a count hint.
        evidence = list(dict.fromkeys(evidence + [int(row["memory_id"].removeprefix("M"))
            for row in candidate["evidence"]]))[:8]
        if not numeric and candidate.get('prior_assessment'):
            # A descriptive addition does not erase a prior estimate; keep its
            # still-readable supporting evidence pinned with the new version.
            from hakimi.memory.memory_store import store as store_factory
            old_ids = _evidence_ids(candidate['prior_assessment'].get('evidence_memory_ids', []))
            with store_factory(str(Path(base).resolve())).db() as db:
                for value in old_ids:
                    row = db.execute("SELECT session,text,state FROM records WHERE id=?", (value,)).fetchone()
                    if row and str(row['session']).startswith('group:') and row['state']=='active' and row['text']:
                        if value not in evidence:
                            evidence.append(value)
                    if len(evidence)>=8:
                        break
        origin = "terra:" + str(review_id) + ":" + key[0] + "->" + key[1]
        try:
            receipt = record_social(
                base, candidate["session"], note, key[1], subject_id=key[0],
                relationship_type=relationship, affinity=scores["affinity"],
                closeness=scores["closeness"], confidence=scores["confidence"],
                evidence_memory_ids=evidence, provider="terra",
                provenance="terra_assisted", reason=reason,
                origin_message_id=origin,
            )
        except (SocialError, sqlite3.Error, OSError) as exc:
            results.append({"direction": candidate["direction"], "status": "failed",
                            "reason": str(getattr(exc,'code',type(exc).__name__))[:80]})
            continue
        results.append({
            "direction": candidate["direction"], "status": "duplicate" if receipt.get("duplicate") else "written",
            "assessment_id": receipt.get("assessment_id"), "numeric": numeric,
            "proposed": {key: item.get(key) for key in ("affinity", "closeness", "confidence")},
            "applied": {"affinity": receipt.get("affinity"),
                        "closeness": receipt.get("closeness"),
                        "confidence": receipt.get("confidence")},
            "policy": receipt.get("affinity_policy") or {},
        })
    return results


def _advance_cursor(state, candidate, now, status, assessment_id=None):
    cursors = state.setdefault("direction_cursors", {})
    if not isinstance(cursors, dict):
        cursors = {}
        state["direction_cursors"] = cursors
    cursors[candidate["direction"]] = {
        "fingerprint": candidate.get("fingerprint"),
        "last_evidence_ids": [row["memory_id"] for row in candidate.get("evidence", [])],
        "max_evidence_id": max((int(row["memory_id"].removeprefix("M")) for row in candidate.get("evidence", [])),default=0),
        "last_run_at": now,
        "last_status": status,
        "last_assessment_id": assessment_id,
    }
    # Bounded per-direction cursor state.
    if len(cursors) > 500:
        for key in sorted(cursors, key=lambda value: float((cursors[value] or {}).get("last_run_at", 0)))[:-500]:
            cursors.pop(key, None)


def run_cycle(base, now=None, *, runner=None, budget=None, force=False):
    """One 2-hour Terra batch.  No QQ output and no model call without work."""
    now = time.time() if now is None else float(now)
    state = _load(base)
    from hakimi.models.model_trial import packet as trial_packet, save_review as save_trial_review, COMPARISON_SCHEMA
    trial=trial_packet(base,now)
    if not force and not (trial and trial.get('final')) and now < float(state.get("next_due", 0) or 0):
        return {"status": "not_due", "next_due": state.get("next_due", 0), "skipped_reason": "not_due",
                "budget_model": False}
    initial_catchup = not state.get('initialized',False)
    state['initialized']=True
    window = 4*3600 if initial_catchup else SAMPLE_WINDOW
    dsh = collect_dsh_samples(base, now, window_seconds=window)
    gemini = collect_gemini_samples(base, now, dsh) if dsh else []
    relations = relation_candidates(base, now, state=state)
    relation_signature = _relation_signature(relations)
    relation_due = bool(relations)
    style_due = bool(dsh and gemini)
    from hakimi.bridge.bot_registry import candidates as bot_candidates,apply_reviews,BOT_REVIEW_SCHEMA
    bots=bot_candidates(base)
    if not style_due and not relation_due and not trial and not bots:
        state.update(next_due=now + CYCLE_SECONDS,
                     last_result={"at": now, "status": "skipped",
                                  "reason": "no_new_dsh_samples" if not dsh else "no_style_baseline"})
        _save(base, state)
        return {"status": "skipped", "skipped_reason": state["last_result"]["reason"],
                "next_due": state["next_due"], "dsh_samples": len(dsh),
                "gemini_samples": len(gemini), "relation_candidates": len(relations),
                "budget_model": False}
    prompt = _prompt(dsh, gemini, relations if relation_due else [], trial=trial,bots=bots)
    submitted = json.loads(prompt.rsplit("\n", 1)[-1])
    relations = submitted.get("relationship_candidates", [])
    relation_due = bool(relations)
    # Recompute support from actual submitted evidence after any prompt trimming.
    for candidate in relations:
        candidate['support'] = len(candidate['evidence'])
        candidate['days'] = sorted({row['date'] for row in candidate['evidence']})
        candidate['numeric_allowed'] = candidate['support'] >= 2
        candidate['confidence_cap'] = _confidence_cap(candidate['support'],candidate['days'])
    relation_signature = _relation_signature(relations)
    signature = _batch_signature(dsh, gemini, relations if relation_due else [])
    if bots:signature=hashlib.sha256((signature+json.dumps(bots,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()
    if trial:signature=hashlib.sha256((signature+trial['signature']+str(trial['final'])).encode()).hexdigest()
    if not force and signature in state.get("reviewed_signatures", []):
        state.update(next_due=now + CYCLE_SECONDS,
                     last_result={"at": now, "status": "skipped", "reason": "duplicate_batch"})
        _save(base, state)
        return {"status": "skipped", "skipped_reason": "duplicate_batch",
                "next_due": state["next_due"], "dsh_samples": len(dsh),
                "gemini_samples": len(gemini), "relation_candidates": len(relations),
                "budget_model": False}
    if runner is None:
        def runner(prompt, schema):
            return run_codex_structured(prompt, base, schema, model=TERRA_MODEL,
                                        timeout=90, max_prompt_chars=MAX_PROMPT)
    if budget is None:
        from hakimi.ops.terra_budget import budget_for
        budget = budget_for(base)
    try:
        receipt = budget.reserve("scheduled", now, estimate=BATCH_RESERVE)
    except BudgetExhausted as exc:
        state.update(next_due=now + CYCLE_SECONDS,
                     last_result={"at": now, "status": "skipped", "reason": str(exc)[:80]})
        _save(base, state)
        return {"status": "skipped", "skipped_reason": str(exc),
                "next_due": state["next_due"], "dsh_samples": len(dsh),
                "gemini_samples": len(gemini), "relation_candidates": len(relations),
                "budget_model": False}
    result = usage = None
    try:
        schema=SCHEMA
        if trial:schema=dict(SCHEMA,properties=dict(SCHEMA['properties'],comparison=COMPARISON_SCHEMA),required=SCHEMA['required']+['comparison'])
        if bots:schema=dict(schema,properties=dict(schema['properties'],bot_reviews=BOT_REVIEW_SCHEMA),required=schema['required']+['bot_reviews'])
        result, usage = runner(prompt, schema)
        budget.settle(receipt, usage, success=True, now=now)
    except Exception as exc:
        budget.settle(receipt, None, success=False, now=now)
        state.update(next_due=now + CYCLE_SECONDS,
                     last_result={"at": now, "status": "failed", "reason": type(exc).__name__})
        # The previous valid feedback file is deliberately left untouched and
        # no direction cursor/signature is advanced, so this evidence can retry.
        _save(base, state)
        return {"status": "failed", "error": type(exc).__name__, "skipped_reason": "analysis_failure",
                "next_due": state["next_due"], "dsh_samples": len(dsh),
                "gemini_samples": len(gemini), "relation_candidates": len(relations),
                "budget_model": True}
    if not isinstance(result, dict) or not isinstance(result.get("relations"), list):
        state.update(next_due=now + CYCLE_SECONDS,
                     last_result={"at": now, "status": "failed", "reason": "malformed_result"})
        _save(base, state)
        return {"status": "failed", "error": "malformed_result", "skipped_reason": "analysis_failure",
                "next_due": state["next_due"], "dsh_samples": len(dsh),
                "gemini_samples": len(gemini), "relation_candidates": len(relations),
                "budget_model": True}
    bots_reviewed=apply_reviews(base,result.get('bot_reviews'),bots,now) if bots else 0
    if trial:save_trial_review(base,trial,result.get('comparison',{}),now)
    feedback_written, feedback_chars = (False, 0)
    style = result.get("style") if isinstance(result, dict) else None
    if style_due and isinstance(style, dict) and style.get("verdict") in {"normal", "problem"}:
        feedback_written, feedback_chars = _write_feedback(base, result)
    relation_results = []
    if relation_due:
        relation_results = apply_relations(base, result.get("relations"), relations, now,
                                           signature[:20])
    by_direction = {item["direction"]: item for item in relation_results}
    written = [item for item in relation_results if item.get("status") in {"written", "duplicate"}]
    retry_pending = [item for item in relation_results if item.get("status") in {"failed", "invalid"}]
    nochange = []
    if relation_due:
        for candidate in relations:
            item = by_direction.get(candidate["direction"])
            if item and item.get('status') in {'failed','invalid'}:
                continue
            if item:
                _advance_cursor(state, candidate, now, item.get("status", "written"),
                                item.get("assessment_id"))
            else:
                # A valid, complete model response that declined this direction
                # is a real no-change: advance the cursor so the same evidence
                # is not sent to the model again.
                _advance_cursor(state, candidate, now, "nochange")
                nochange.append(candidate["direction"])
        state["last_relation_signature"] = relation_signature
    if not retry_pending:
        state["reviewed_signatures"] = (state.get("reviewed_signatures", []) + [signature])[-48:]
    state.update(next_due=now + CYCLE_SECONDS, last_review_at=now, last_signature=signature,
                 last_review_id=signature[:20],
                 last_feedback_at=now if feedback_written else state.get("last_feedback_at", 0),
                 last_result={"at": now, "status": "completed",
                              "style": bool(feedback_written), "bots_reviewed":bots_reviewed,"relations": len(written),
                              "relations_nochange": len(nochange), "retry_pending": len(retry_pending),
                              "retry_reasons": [{k:item.get(k) for k in ('direction','status','reason')} for item in retry_pending],
                              "sample_window_seconds": window, "initial_catchup": initial_catchup})
    _save(base, state)
    return {"status": "completed", "next_due": state["next_due"],
            "dsh_samples": len(dsh), "gemini_samples": len(gemini),
            "relation_candidates": len(relations), "relations_written": len(written),
            "relations_nochange": len(nochange), "retry_pending": len(retry_pending),
            "feedback_written": feedback_written, "feedback_chars": feedback_chars,"bots_reviewed":bots_reviewed,
            "budget_model": True}


def report(base, now=None):
    """Counts/next-due only; never includes samples, messages or tokens."""
    now = time.time() if now is None else float(now)
    state = _load(base)
    return {
        "next_due": state.get("next_due", 0),
        "due": now >= float(state.get("next_due", 0) or 0),
        "last_result": state.get("last_result"),
        "reviewed_batches": len(state.get("reviewed_signatures", [])),
        "last_review_at": state.get("last_review_at", 0),
        "last_feedback_at": state.get("last_feedback_at", 0),
        "feedback_present": _feedback_path(base).is_file(),
        "counts_only": True,
    }
