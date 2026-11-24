"""Read side of social assessments: latest per pair/direction, person relations, evidence."""
from __future__ import annotations

import json
import sqlite3

from hakimi.social.social_base import PERSON_ID, _assessment_dict, _utc_day
from hakimi.social.social_fields import MAX_EVIDENCE


def latest_assessments(db, *, session=None, active_only=True):
    """Return latest-version assessments with surviving evidence filled in.

    ``session=None`` means the whole public-group scope, never private chats.
    A caller may explicitly pass ``private:<id>`` to inspect that conversation.
    """
    clauses = []
    params = []
    if active_only:
        clauses.append("state='active'")
    if session:
        clauses.append("session=?")
        params.append(session)
    else:
        clauses.append("session LIKE 'group:%'")
    clause = " WHERE " + " AND ".join(clauses)
    try:
        rows = db.execute("SELECT * FROM social_assessments" + clause +
                          " ORDER BY created_at DESC,id DESC", params).fetchall()
    except sqlite3.OperationalError as exc:
        # A read-only v3 database may predate the social projection.  Read
        # helpers never create tables just to answer a query.
        if "no such table" in str(exc).lower():
            return []
        raise
    result = []
    for row in rows:
        evidence_ids = []
        try:
            evidence_ids = [int(value) for value in json.loads(row["evidence_record_ids"] or "[]")]
        except (TypeError, ValueError):
            pass
        evidence_rows = []
        if evidence_ids:
            marks = ",".join("?" for _ in evidence_ids)
            evidence_rows = db.execute(
                "SELECT id,user_id,session,kind,at,state,text FROM records WHERE id IN (" + marks + ") "
                "AND state!='deleted'", evidence_ids,
            ).fetchall()
        result.append(_assessment_dict(row, evidence_rows=evidence_rows))
    return result


def latest_by_direction(assessments):
    """Newest active record per direction and provenance, newest first."""
    result = []
    seen = set()
    for item in sorted(assessments, key=lambda value: (float(value.get("created_at") or 0), int(value.get("id") or 0)),
                       reverse=True):
        key = (str(item["subject_id"]), str(item["target_id"]), str(item["provenance"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def person_social_relations(db, person_id, *, session=None, cross_groups=False):
    """Compact, scoped relation values for the bot's ``memory.graph`` focus."""
    person = str(person_id)
    if not PERSON_ID.fullmatch(person):
        return []
    # A private caller may not use the public "cross groups" flag to widen its
    # scope; only an explicitly public/group session may search all groups.
    private_scope = str(session or "").startswith("private:")
    if cross_groups and not private_scope:
        assessments = latest_by_direction(latest_assessments(db, session=None))
    else:
        assessments = latest_by_direction(latest_assessments(db, session=session))
    result = []
    for item in assessments:
        if person not in (str(item["subject_id"]), str(item["target_id"])):
            continue
        result.append({
            "assessment_id": item["id"],
            "direction": item["direction"],
            "subject_id": item["subject_id"],
            "target_id": item["target_id"],
            "relationship_type": item["relationship_type"],
            "provenance": item["provenance"],
            "provenance_label": item["provenance_label"],
            "author": item["author"],
            "affinity": item["affinity"],
            "closeness": item["closeness"],
            "confidence": item["confidence"],
            "scores_available": item["scores_available"],
            "scores_note": item["scores_note"],
            "reason": item["reason"],
            "evidence_memory_ids": item["evidence_memory_ids"][:MAX_EVIDENCE],
            "evidence_dates": item["evidence_dates"],
            "evidence_times": item["evidence_times"],
            "created_at": item["created_at"],
            "expires_at": item["expires_at"],
            "stale": item["stale"],
            "session": item["session"],
            "original_memory_id": item["original_memory_id"],
        })
    return result[:12]


def assessment_detail(db, assessment_id):
    row = db.execute("SELECT * FROM social_assessments WHERE id=?", (int(assessment_id),)).fetchone()
    if not row:
        return None
    evidence_ids = []
    try:
        evidence_ids = [int(value) for value in json.loads(row["evidence_record_ids"] or "[]")]
    except (TypeError, ValueError):
        pass
    rows = []
    if evidence_ids:
        marks = ",".join("?" for _ in evidence_ids)
        rows = db.execute(
            "SELECT id,user_id,session,kind,at,state,text,nickname FROM records WHERE id IN (" + marks + ") "
            "AND state!='deleted' ORDER BY at DESC", evidence_ids,
        ).fetchall()
    return _assessment_dict(row, evidence_rows=rows)


def assessment_evidence(db, assessment):
    """Return safe evidence excerpts for a projection, preserving M ids."""
    ids = []
    for value in assessment.get("evidence_memory_ids") or []:
        text = str(value).upper().removeprefix("M")
        if text.isdigit():
            ids.append(int(text))
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        "SELECT id,user_id,session,kind,at,text,state FROM records WHERE id IN (" + marks + ") "
        "AND state!='deleted' ORDER BY at DESC", ids,
    ).fetchall()
    return [{
        "memory_id": "M" + str(row["id"]), "user_id": row["user_id"], "session": row["session"],
        "kind": row["kind"], "at": row["at"], "date": _utc_day(row["at"]),
        "excerpt": (str(row["text"] or "")[:260] if str(row["text"] or "").strip()
                    else "（证据原文已被清理）"),
        "expired": not bool(str(row["text"] or "").strip()),
    } for row in rows]
