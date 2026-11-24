"""Social impression fields: validation, visible evidence, score inheritance rules and lookups."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from hakimi.social.social_base import PERSON_ID, TERRA_PROVENANCE, _evidence_involves
from hakimi.social.social_schema import SCHEMA_INDEXES, SCHEMA_TABLE


HAKIMI_SUBJECT = "hakimi"
DEFAULT_PROVENANCE = "gemini_manual"
SEED_PROVENANCE = "dsh_assisted"
PROVENANCES = {"gemini_manual", "dsh_assisted", TERRA_PROVENANCE}
SESSIONS = re.compile(r"(?:group|private):[1-9][0-9]{0,19}")
# ``MemoryStore.remember_manual`` keeps at most eight related M ids.  The social
# projection must not advertise more evidence than the original writer stores.
MAX_EVIDENCE = 8
SCHEMA = SCHEMA_TABLE + SCHEMA_INDEXES


class SocialError(ValueError):
    """A user-facing validation or provenance error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _score(value, field):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise SocialError("invalid_score", f"{field}必须是0到100的整数或留空")
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}", value.strip()):
        value = int(value.strip())
    if not isinstance(value, int) or not 0 <= value <= 100:
        raise SocialError("invalid_score", f"{field}必须是0到100的整数或留空")
    return value


def _clean_text(value, field, *, maximum, allow_empty=True):
    if not isinstance(value, str):
        raise SocialError("invalid_text", f"{field}必须是文本")
    # Windows CRLF is normal note text and must be preserved byte-for-byte.
    if any(ord(ch) < 32 and ch not in "\r\n\t" for ch in value) or "\x7f" in value:
        raise SocialError("invalid_text", f"{field}含控制字符")
    if not allow_empty and not value.strip():
        raise SocialError("invalid_text", f"{field}不能为空")
    if maximum is not None and len(value) > maximum:
        raise SocialError("invalid_text", f"{field}最多{maximum}字")
    return value


def _evidence_ids(values):
    if values is None or values == "":
        return []
    if isinstance(values, str):
        parts = re.split(r"[,，\s]+", values.strip())
    elif isinstance(values, (list, tuple)):
        parts = list(values)
    else:
        raise SocialError("invalid_evidence", "evidence_memory_ids必须是M编号列表")
    result = []
    for part in parts:
        text = str(part).strip().upper().removeprefix("M")
        if not text.isdigit() or not re.fullmatch(r"[1-9][0-9]{0,19}", text):
            raise SocialError("invalid_evidence", "证据必须是真实存在的M编号")
        result.append(int(text))
    result = list(dict.fromkeys(result))
    if len(result) > MAX_EVIDENCE:
        raise SocialError("invalid_evidence", f"证据最多{MAX_EVIDENCE}条")
    return result


def validate_fields(*, note, target_id, subject_id=HAKIMI_SUBJECT, relationship_type="",
                    affinity=None, closeness=None, confidence=None, evidence_memory_ids=(),
                    session="", provider="gemini", provenance=DEFAULT_PROVENANCE,
                    impression=None, reason=""):
    """Validate without touching the database; returns a normalised dict."""
    if not isinstance(session, str) or not SESSIONS.fullmatch(session):
        raise SocialError("invalid_session", "session必须是group:数字或private:数字")
    target = str(target_id or "").strip()
    if not PERSON_ID.fullmatch(target):
        raise SocialError("invalid_target", "target_id必须是官方人物ID")
    subject = str(subject_id or HAKIMI_SUBJECT).strip()
    if subject != HAKIMI_SUBJECT and not PERSON_ID.fullmatch(subject):
        raise SocialError("invalid_subject", "subject_id只能是hakimi或官方人物ID")
    if subject == target:
        raise SocialError("invalid_pair", "subject_id和target_id不能相同")
    if provenance not in PROVENANCES:
        raise SocialError("invalid_provenance", "provenance必须是gemini_manual、dsh_assisted或terra_assisted")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", str(provider or "")):
        raise SocialError("invalid_provider", "provider格式无效")
    relationship = _clean_text(relationship_type or "关系印象", "relationship_type", maximum=40)
    if not relationship.strip():
        raise SocialError("invalid_relationship", "relationship_type不能为空")
    text = _clean_text(note if note is not None else impression, "note", maximum=None, allow_empty=False)
    reason_text = _clean_text(reason or "", "reason", maximum=1000)
    evidence = _evidence_ids(evidence_memory_ids)
    return {
        "note": text,
        "target_id": target,
        "subject_id": subject,
        "relationship_type": relationship.strip(),
        "affinity": _score(affinity, "affinity"),
        "closeness": _score(closeness, "closeness"),
        "confidence": _score(confidence, "confidence"),
        "evidence": evidence,
        "session": session,
        "provider": str(provider),
        "provenance": provenance,
        "reason": reason_text,
        "author": ("terra" if provenance == TERRA_PROVENANCE
                   else "dsh" if provenance == SEED_PROVENANCE else "gemini"),
    }


def _scope(session):
    """Strict social scope: group memories may cross groups; private stays private."""
    if str(session).startswith("group:"):
        return True, "r.state!='deleted' AND r.kind!='dialogue' AND r.session LIKE 'group:%'", []
    return False, "r.state!='deleted' AND r.kind!='dialogue' AND r.session=?", [str(session)]


def _load_visible_evidence(db, store, session, ids, cross):
    if not ids:
        return []
    _, scope_sql, params = _scope(session)
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        "SELECT r.id,r.user_id,r.session,r.kind,r.at,r.state,r.text,r.direct_targets,r.reply_to "
        "FROM records r WHERE r.id IN (" + marks + ") AND " + scope_sql,
        [*ids, *params],
    ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    return [by_id[value] for value in ids if value in by_id]


def _inheritance_eligible(fields):
    """Both live backends write Hakimi’s own judgment; keep audit provenance."""
    return (
        fields.get("subject_id") == HAKIMI_SUBJECT
        and (str(fields.get("provider") or "").lower(), fields.get("provenance"))
            in {("gemini", DEFAULT_PROVENANCE), ("dsh", SEED_PROVENANCE)}
    )


def _assessment_scope(session):
    if str(session).startswith("group:"):
        return "session LIKE 'group:%'", []
    return "session=?", [str(session)]


def _assessment_evidence_ids(row):
    try:
        return [int(value) for value in json.loads(row["evidence_record_ids"] or "[]")]
    except (TypeError, ValueError, IndexError, KeyError):
        return []


def _valid_evidence_rows(db, session, ids, now):
    """Read only still-verifiable evidence in the requested social scope.

    Public groups may corroborate each other; a private chat is exact-session
    only.  Deleted/retracted rows, dialogue rows, cleared text, and expired
    rows never count as verifiable evidence for an inherited score.
    """
    values = []
    for value in ids or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            return []
        if number not in values:
            values.append(number)
    if not values:
        return []
    public = str(session).startswith("group:")
    scope_sql = "r.session LIKE 'group:%'" if public else "r.session=?"
    params = [] if public else [str(session)]
    marks = ",".join("?" for _ in values)
    base = (
        "SELECT r.id,r.user_id,r.session,r.kind,r.at,r.state,r.text,"
        "r.direct_targets,r.reply_to,r.expires FROM records r "
        "WHERE r.id IN (" + marks + ") AND r.state NOT IN ('deleted','retracted') "
        "AND r.kind!='dialogue' AND " + scope_sql + " "
    )
    try:
        rows = db.execute(base + "AND (r.expires=0 OR r.expires>?)",
                          [*values, *params, float(now)]).fetchall()
    except sqlite3.OperationalError:
        # Very old/simplified fixtures may not have the expiry column yet.
        rows = db.execute(base, [*values, *params]).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    return [by_id[value] for value in values if value in by_id]


def _manual_source(db, fields, row, now):
    """Validate identity, provenance and scope of a durable score ancestor."""
    if row is None:
        return None
    try:
        if str(row["subject_id"]) != fields["subject_id"]:
            return None
        if str(row["target_id"]) != fields["target_id"]:
            return None
        if not _inheritance_eligible(dict(row)):
            return None
        state = str(row["state"] or "active").lower()
        if state not in ("active", "superseded"):
            return None
        source_session = str(row["session"] or "")
        if str(fields["session"]).startswith("group:"):
            if not source_session.startswith("group:"):
                return None
        elif source_session != str(fields["session"]):
            return None
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    evidence_ids = _assessment_evidence_ids(row)
    evidence_rows = _valid_evidence_rows(db, fields["session"], evidence_ids, now)
    # Evidence may be pruned after a legitimate score was committed. Keep the
    # durable number and its historical ids; merge only readable rows into notes.
    return {
        "row": row,
        "source_assessment_id": int(row["id"]),
        "evidence_ids": evidence_ids,
        "evidence_rows": evidence_rows,
        "session": source_session,
        "created_at": float(row["created_at"] or 0),
    }


def _find_manual_sources(db, fields, now):
    """Newest durable Gemini-manual score per field in the allowed scope.

    The lookup never mixes provenance: Terra/DSH rows are excluded before any
    value is read.  A valid same-session source is preferred; public groups may
    then corroborate each other, while private chats never leave their session.
    """
    if not _inheritance_eligible(fields):
        return {}
    scope_sql, params = _assessment_scope(fields["session"])
    try:
        rows = db.execute(
            "SELECT * FROM social_assessments WHERE subject_id=? AND target_id=? "
            "AND ((lower(provider)='gemini' AND provenance='gemini_manual') "
            "OR (lower(provider)='dsh' AND provenance='dsh_assisted')) "
            "AND lower(COALESCE(state,'active')) IN ('active','superseded') "
            "AND " + scope_sql + " ORDER BY created_at DESC,id DESC LIMIT 256",
            [fields["subject_id"], fields["target_id"], *params],
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    sources, blocked = {}, set()
    for row in rows:
        source = _manual_source(db, fields, row, now)
        for field in ("affinity", "closeness", "confidence"):
            if field in sources or field in blocked or row[field] is None:
                continue
            if source is None:
                # A newer scored judgment lost its evidence: do not revive an
                # older conflicting number by silently skipping this barrier.
                blocked.add(field)
            else:
                sources[field] = source
        if len(set(sources) | blocked) == 3:
            break
    return sources


def _merge_inherited_evidence(current_ids, inherited, sources):
    """Keep inherited score evidence and current evidence, capped at eight."""
    merged, remaining = [], []
    # Retain a readable anchor for each inherited score source, then current
    # evidence, then extra historical excerpts. New interactions must not be
    # crowded out by an old eight-excerpt projection.
    for field in ("affinity", "closeness", "confidence"):
        source = sources.get(field) if field in inherited else None
        if not source:
            continue
        anchors = [int(r["id"]) for r in source["evidence_rows"]
                   if _evidence_involves(r, str(source["row"]["target_id"]))]
        if anchors and anchors[0] not in merged:
            merged.append(anchors[0])
        remaining.extend(int(row["id"]) for row in source["evidence_rows"])
    for value in list(current_ids or ()) + remaining:
        if int(value) not in merged:
            merged.append(int(value))
    return merged[:MAX_EVIDENCE]


def _inheritance_metadata(inherited):
    details = {}
    for field, source in inherited.items():
        details[field] = {
            "source_assessment_id": int(source["source_assessment_id"]),
            "value": int(source["row"][field]),
            "source_session": source["session"],
            "source_created_at": source["created_at"],
            "evidence_memory_ids": ["M" + str(value) for value in source["evidence_ids"]],
            "inherited": True,
            "rescored": False,
        }
    return {
        "inherited": details,
        "inherited_scores": details,
        "inherited_fields": sorted(details),
        "source_assessment_ids": {
            field: item["source_assessment_id"] for field, item in details.items()
        },
        "inheritance_note": (
            "新笔记没有重新打分；缺少的分数字段沿用同人物同作用域内"
            "已记入的旧值；证据清理不会使分数退回未知。"
        ),
    }


def _with_inheritance_policy(policy, fields, inherited, requested, impact):
    result = dict(policy or {})
    result.update(_inheritance_metadata(inherited))
    if requested.get("affinity") is None and "affinity" in inherited:
        value = int(inherited["affinity"]["row"]["affinity"])
        result.update({
            "previous": value,
            "proposed": None,
            "applied": value,
            "delta": 0,
            "impact": impact or "ordinary",
            "policy_reason": (
                "本次未给新分数，沿用同人物同作用域内已记入的旧评分；"
                "没有重新打分，原文照常保存。"
            ),
        })
    elif not result.get("policy_reason"):
        previous = None
        if "affinity" in inherited:
            previous = int(inherited["affinity"]["row"]["affinity"])
        applied = fields.get("affinity")
        result.update({
            "previous": previous,
            "proposed": requested.get("affinity"),
            "applied": applied,
            "delta": (0 if previous is None or applied is None
                      else int(applied) - int(previous)),
            "impact": impact or "ordinary",
            "policy_reason": "缺少的分数字段沿用已记入的旧值；未重新打分。",
        })
    return result


def _request_id(fields, origin):
    identity = json.dumps({
        "session": fields["session"], "target": fields["target_id"], "subject": fields["subject_id"],
        "relationship": fields["relationship_type"], "note": fields["note"],
        "affinity": fields["affinity"], "closeness": fields["closeness"],
        "confidence": fields["confidence"], "evidence": fields["evidence"],
        "provenance": fields["provenance"], "provider": fields["provider"], "origin": origin,
        "reason": fields["reason"],
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _existing(db, request_id):
    row = db.execute("SELECT * FROM social_assessments WHERE request_id=?", (request_id,)).fetchone()
    return row


def _latest_active(db, fields):
    """Latest active projection for one session/pair/provenance/nature."""
    return db.execute(
        "SELECT * FROM social_assessments WHERE session=? AND subject_id=? AND target_id=? "
        "AND provenance=? AND relationship_type=? AND state='active' "
        "ORDER BY created_at DESC,id DESC LIMIT 1",
        (fields["session"], fields["subject_id"], fields["target_id"],
         fields["provenance"], fields["relationship_type"]),
    ).fetchone()


def _find_prior_impression(db, fields):
    """One person's notebook continues across public groups and their DM."""
    return db.execute(
        "SELECT * FROM social_assessments WHERE (session LIKE 'group:%' OR session=?) "
        "AND subject_id=? AND target_id=? AND provenance=? AND state='active' "
        "ORDER BY created_at DESC,id DESC LIMIT 1",
        ('private:'+fields['target_id'], fields['subject_id'], fields['target_id'], fields['provenance']),
    ).fetchone()


def _append_impression_text(prior_text, extra_text, *, max_chars=360):
    base_text = str(prior_text or "").rstrip()
    extra = str(extra_text or "").strip()
    if not base_text:
        return str(extra_text or "")
    if not extra or extra in base_text:
        return base_text
    sep = "" if base_text[-1] in "。！？!?…~；;\n" else "。"
    concatenated = base_text + sep + extra
    from hakimi.social.impression_forgetting import compact_impression_text, is_boilerplate_clause, split_clauses
    has_boilerplate = any(is_boilerplate_clause(c) for c in split_clauses(base_text))
    if len(concatenated) <= max_chars and not has_boilerplate:
        return concatenated
    return compact_impression_text(base_text, extra_text=extra, max_chars=max_chars, keep_recent=2)


def _same_revision(row, fields):
    try:
        evidence = [int(value) for value in json.loads(row["evidence_record_ids"] or "[]")]
    except (TypeError, ValueError):
        evidence = []
    return (
        row["impression"] == fields["note"]
        and row["affinity"] == fields["affinity"]
        and row["closeness"] == fields["closeness"]
        and row["confidence"] == fields["confidence"]
        and (row["reason"] or "") == fields["reason"]
        and row["relationship_type"] == fields["relationship_type"]
        and row["session"] == fields["session"]
        and evidence == fields["evidence"]
    )


def _stable_pair_key(fields):
    digest = hashlib.sha256(
        (fields["subject_id"] + "->" + fields["target_id"]).encode("utf-8")
    ).hexdigest()[:10]
    return f"关系印象·{fields['relationship_type']}·{digest}"[:64]
