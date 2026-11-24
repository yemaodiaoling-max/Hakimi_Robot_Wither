"""Social record primitives shared by impressions and affinity curves: provenance labels, evidence targeting, assessment rows."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone


TERRA_PROVENANCE = "terra_assisted"
PROVENANCE_LABELS = {
    "gemini_manual": "Gemini 亲笔",
    "dsh_assisted": "DSH 辅助整理",
    TERRA_PROVENANCE: "Terra 观察（辅助，非当事人内心）",
}
PERSON_ID = re.compile(r"[1-9][0-9]{0,19}")
CQ_AT = re.compile(r"\[CQ:at,qq=([1-9][0-9]{0,19})(?:,[^\]]*)?\]")
CQ_REPLY = re.compile(r"\[CQ:reply,id=(-?[0-9]{1,20})(?:,[^\]]*)?\]")
QUOTE_STARTS = ("[引用", "[回复", "[转发", "[合并转发", "[CQ:forward", "[CQ:json", "[CQ:xml")


def _utc_day(at):
    try:
        return datetime.fromtimestamp(float(at), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _current_text(value):
    """Return only the current speaker's text, not quoted/forwarded insides.

    Old databases often retain CQ codes in ``records.text``.  A forward or a
    quoted message may itself contain ``[CQ:at,...]``; those nested mentions do
    not belong to the current speaker and must not become direct interactions.
    """
    text = str(value or "")
    if not any(prefix in text for prefix in QUOTE_STARTS):
        return text
    output = []
    index = 0
    while index < len(text):
        start = next((prefix for prefix in QUOTE_STARTS if text.startswith(prefix, index)), None)
        if not start:
            output.append(text[index])
            index += 1
            continue
        depth = 0
        cursor = index
        while cursor < len(text):
            character = text[cursor]
            if character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
                if depth == 0:
                    cursor += 1
                    break
            cursor += 1
        index = cursor
    return "".join(output)


def _text_targets(value):
    return list(dict.fromkeys(CQ_AT.findall(_current_text(value))))


def _reply_target(value):
    match = CQ_REPLY.search(_current_text(value))
    return match.group(1) if match else ""


def _evidence_text_available(row):
    try:
        return bool(str(row["text"] or "").strip())
    except (IndexError, KeyError):
        return False


def _direct_targets(value):
    if isinstance(value, (list, tuple)):
        return {str(item) for item in value if PERSON_ID.fullmatch(str(item))}
    if not isinstance(value, str) or not value.strip():
        return set()
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return {str(item) for item in parsed if PERSON_ID.fullmatch(str(item))}
    except (TypeError, ValueError):
        pass
    return {item for item in re.findall(r"[1-9][0-9]{0,19}", value)}


def _row_targets(row):
    """Explicit metadata plus old-style current-message CQ @ targets."""
    targets = set(_direct_targets(row["direct_targets"]))
    targets.update(_text_targets(row["text"]))
    return {value for value in targets if PERSON_ID.fullmatch(value)}


def _evidence_involves(row, person, db=None):
    person = str(person)
    if str(row["user_id"]) == person:
        return True
    if person in _row_targets(row):
        return True
    if db is not None and "reply_to" in row.keys():
        reply = str(row["reply_to"] or "") or _reply_target(str(row["text"] or ""))
        if reply:
            referenced = db.execute("SELECT user_id FROM records WHERE session=? AND origin=? "
                "AND kind='chat' AND state NOT IN ('deleted','retracted') LIMIT 1",
                (row["session"], reply)).fetchone()
            if referenced and str(referenced[0]) == person:
                return True
    return False


def _assessment_dict(row, *, evidence_rows=None):
    try:
        evidence = json.loads(row["evidence_record_ids"] or "[]")
    except (TypeError, ValueError):
        evidence = []
    try:
        dates = json.loads(row["evidence_dates"] or "[]")
    except (TypeError, ValueError):
        dates = []
    try:
        times = json.loads(row["evidence_times"] or "[]")
    except (TypeError, ValueError):
        times = []
    try:
        expires_at = float(row["expires_at"] or 0)
    except (TypeError, ValueError, IndexError, KeyError):
        expires_at = 0.0
    provenance = str(row["provenance"])
    available = []
    missing = []
    expired = []
    visible = []
    if evidence_rows is not None:
        by_id = {int(item["id"]): item for item in evidence_rows}
        for value in evidence:
            value = int(value)
            if value not in by_id:
                missing.append(value)
                continue
            visible.append(value)
            if not _evidence_text_available(by_id[value]):
                # Retention may erase the raw excerpt while leaving the row.
                # Evidence availability is reported separately from durable scores.
                expired.append(value)
            else:
                available.append(value)
    # Terra staleness is evidence-driven only.  Old expires_at values are kept
    # as history but never age a durable social observation out of the
    # projection; missing/deleted evidence or supersession makes it stale.
    stale = bool(provenance == TERRA_PROVENANCE and evidence_rows is not None and (
        bool(missing) or bool(expired) or str(row["state"] or "active") != "active"
        or row["superseded_by"] is not None))
    # A committed judgment is durable. Retention changes evidence availability,
    # not the person's recorded score; later curve updates start from this value.
    scores_available = True
    return {
        "id": int(row["id"]),
        "subject_id": row["subject_id"],
        "target_id": row["target_id"],
        "direction": row["subject_id"] + "->" + row["target_id"],
        "author": row["author"],
        "provider": row["provider"],
        "provenance": provenance,
        "provenance_label": PROVENANCE_LABELS.get(provenance, "未标注来源"),
        "impression": row["impression"],
        "reason": row["reason"] or "",
        "affinity": row["affinity"] if scores_available else None,
        "closeness": row["closeness"] if scores_available else None,
        "confidence": row["confidence"] if scores_available else None,
        "relationship_type": row["relationship_type"],
        "evidence_memory_ids": ["M" + str(value) for value in visible] if evidence_rows is not None
                               else ["M" + str(value) for value in evidence],
        "evidence_dates": dates,
        "evidence_times": times,
        "evidence_missing": ["M" + str(value) for value in missing],
        "evidence_expired": ["M" + str(value) for value in expired],
        "affinity_policy": _stored_affinity_policy(row),
        "scores_available": scores_available,
        "scores_note": ("证据原文已不可核对；保留已记入的评分" if missing or expired else ""),
        "expires_at": 0.0 if provenance == TERRA_PROVENANCE else expires_at,
        "legacy_expires_at": (expires_at if provenance == TERRA_PROVENANCE and expires_at else None),
        "stale": stale,
        "original_memory_id": "M" + str(row["original_record_id"]) if row["original_record_id"] else None,
        "created_at": row["created_at"],
        "session": row["session"],
        "version": row["version"],
        "supersedes_id": row["supersedes_id"],
        "superseded_by": row["superseded_by"],
        "state": row["state"],
        "seed_id": row["seed_id"] or "",
    }


def _stored_affinity_policy(row):
    try:
        data = json.loads(row['affinity_policy'] or '{}')
        return data if isinstance(data, dict) else {}
    except (IndexError, KeyError, ValueError, TypeError):
        return {}
