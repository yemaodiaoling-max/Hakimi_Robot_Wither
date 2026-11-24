"""Record a social impression (note text first, then the provenance-separated assessment)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from hakimi.social.social_base import TERRA_PROVENANCE, _evidence_involves, _stored_affinity_policy, _utc_day
from hakimi.social.social_fields import DEFAULT_PROVENANCE, HAKIMI_SUBJECT, SocialError, _append_impression_text, _existing, _find_manual_sources, _find_prior_impression, _inheritance_eligible, _latest_active, _load_visible_evidence, _merge_inherited_evidence, _request_id, _same_revision, _scope, _stable_pair_key, _with_inheritance_policy, validate_fields
from hakimi.social.social_schema import ensure_schema


def record_social(base, session, note, target_id, *, subject_id=HAKIMI_SUBJECT,
                  relationship_type="", affinity=None, closeness=None, confidence=None,
                  evidence_memory_ids=(), provider="gemini", provenance=DEFAULT_PROVENANCE,
                  reason="", origin_message_id="", seed_id="", now=None, impact=None,
                  mode="replace"):
    """Store an exact note and its social projection.

    The note is written through ``MemoryStore.remember_manual`` first, so its
    original UTF-8 text and hash are immutable.  A failed projection never
    rewrites that text.  Replays return the same ids; a changed body creates a
    new version while the previous assessment stays available as superseded.
    """
    if mode not in {"replace", "append"}:
        raise SocialError("invalid_mode", "mode必须是replace或append")
    explicit_relationship = bool(str(relationship_type or "").strip())
    fields = validate_fields(
        note=note, target_id=target_id, subject_id=subject_id,
        relationship_type=relationship_type, affinity=affinity, closeness=closeness,
        confidence=confidence, evidence_memory_ids=evidence_memory_ids, session=session,
        provider=provider, provenance=provenance, reason=reason,
    )
    # Live notebooks supply the judgment text; event classification owns new scores.
    event_scoring = _inheritance_eligible(fields) and not seed_id
    if event_scoring:
        for field in ('affinity','closeness','confidence'):fields[field]=None
    raw_note = fields["note"]
    if impact is not None and impact not in {'minor', 'ordinary', 'major'}:
        raise SocialError('invalid_impact', 'impact必须是minor/ordinary/major')
    curve_enabled = impact is not None and _inheritance_eligible(fields) and not event_scoring
    requested_scores = {field: fields[field]
                        for field in ("affinity", "closeness", "confidence")}
    requested_evidence = list(fields["evidence"])
    scoring_warning = ""
    if (not fields["evidence"] and _inheritance_eligible(fields)
            and (fields["affinity"] is not None or fields["closeness"] is not None)):
        # A missing scoring basis must not discard a legitimate notebook entry.
        # Ignore only the unsupported new proposal; durable prior scores inherit.
        scoring_warning = "新评分缺少真实证据，未应用；文字已保存，已有评分沿用"
        for field in ("affinity", "closeness", "confidence"):
            fields[field] = None
            requested_scores[field] = None
    inherited = {}
    sources = {}
    affinity_policy = {}
    terra_policy = {}
    now = time.time() if now is None else float(now)
    from hakimi.memory.memory_store import store as store_factory
    index = store_factory(str(Path(base).resolve()))
    cross, scope_sql, params = _scope(session)
    with index.db() as db:
        ensure_schema(db)
        target_known = db.execute(
            "SELECT 1 FROM records r WHERE r.user_id=? AND r.kind='chat' AND r.state!='deleted' AND "
            + scope_sql + " LIMIT 1", [fields["target_id"], *params],
        ).fetchone()
        if not target_known:
            raise SocialError("target_not_visible", "目标人物不在当前可读范围内，不能用自由姓名合并身份")
        if fields["subject_id"] != HAKIMI_SUBJECT:
            subject_known = db.execute(
                "SELECT 1 FROM records r WHERE r.user_id=? AND r.kind='chat' AND r.state!='deleted' AND "
                + scope_sql + " LIMIT 1", [fields["subject_id"], *params],
            ).fetchone()
            if not subject_known:
                raise SocialError("subject_not_visible", "subject_id不在当前可读范围内")
        prior_active = _find_prior_impression(db, fields)
        if prior_active is not None:
            if not explicit_relationship and str(prior_active["relationship_type"] or "").strip():
                fields["relationship_type"] = str(prior_active["relationship_type"]).strip()
            if mode == "append":
                fields["note"] = _append_impression_text(prior_active["impression"], raw_note)
        evidence_rows = _load_visible_evidence(db, index, session, fields["evidence"], cross)
        if len(evidence_rows) != len(fields["evidence"]):
            raise SocialError("evidence_unreadable", "evidence_memory_ids含不存在、已删除或当前不可读的证据")
        if not evidence_rows:
            # A pure subjective note may stand without evidence only when Gemini
            # herself writes it about herself (subject=hakimi) and leaves the
            # relationship scores unknown.  DSH/Terra and any scored update keep
            # the old evidence requirement.
            no_evidence_ok = (
                _inheritance_eligible(fields)
                and fields["affinity"] is None
                and fields["closeness"] is None
            )
            if not no_evidence_ok:
                raise SocialError("evidence_required",
                                  "无证据文字笔记可由当前哈基米后端记录；其他人物关系或辅助观察需真实可见证据")
        else:
            if not any(_evidence_involves(row, fields["target_id"], db) for row in evidence_rows):
                raise SocialError("evidence_not_about_target", "证据中没有目标人物的发言或@记录")
            if fields["subject_id"] != HAKIMI_SUBJECT and not any(
                    _evidence_involves(row, fields["subject_id"], db) for row in evidence_rows):
                raise SocialError("evidence_not_about_subject", "关系证据中没有subject_id的发言或@记录")
        # A Gemini-manual note that leaves a score blank inherits the newest
        # durable Gemini-manual value in the same social scope.  This happens
        # before request identity and before the immutable original is written,
        # so retries are idempotent and the note's text/hash never changes.
        if _inheritance_eligible(fields):
            sources = _find_manual_sources(db, fields, now)
            for field in ("affinity", "closeness", "confidence"):
                if fields[field] is None and field in sources:
                    fields[field] = int(sources[field]["row"][field])
                    inherited[field] = sources[field]
            if inherited:
                fields["evidence"] = _merge_inherited_evidence(
                    fields["evidence"], inherited, sources)
                evidence_rows = _load_visible_evidence(
                    db, index, session, fields["evidence"], cross)
                if len(evidence_rows) != len(fields["evidence"]):
                    raise SocialError(
                        "inherited_evidence_unreadable",
                        "沿用的旧评分证据已不可核对，未建立继承评分")
        origin = str(origin_message_id or ("seed:" + seed_id if seed_id else "auto:" + str(time.time_ns())))
        # Identity uses the raw proposal before any policy adjustment; retries
        # must return the original applied receipt rather than apply twice.
        request_origin = origin + ('|curve:' + impact if curve_enabled else '') + ('|mode:append' if mode == 'append' else '')
        request_id = _request_id({**fields, "note": raw_note, **requested_scores, "evidence": requested_evidence}, request_origin)
        existing = _existing(db, request_id)
        if existing:
            return _duplicate_receipt(db, existing)
        latest = _latest_active(db, fields)
        if latest and _same_revision(latest, fields):
            return _duplicate_receipt(db, latest)

    # The immutable original is written before the relation projection.  An
    # interrupted projection can therefore be repaired by re-running the same
    # request without changing the original text.
    proposal = {
        "kind": "person" if (fields["provenance"] == DEFAULT_PROVENANCE or (_inheritance_eligible(fields) and not seed_id)) else "reflection",
        "note": fields["note"],
        "key": _stable_pair_key(fields),
        "evidence": "",
        "topics": ["关系印象", fields["relationship_type"]],
        "related_memory_ids": ["M" + str(value) for value in fields["evidence"]],
    }
    if fields["provenance"] == DEFAULT_PROVENANCE or (_inheritance_eligible(fields) and not seed_id):
        proposal["user_id"] = fields["target_id"]
    receipt = index.remember_manual(
        proposal, session=session, provider=fields["provider"],
        channel=("gemini_tool" if fields["provenance"] == DEFAULT_PROVENANCE
                 else "terra_tool" if fields["provenance"] == TERRA_PROVENANCE
                 else "social_seed"),
        now=now, origin_message_id=origin,
    )
    if not receipt.get("saved"):
        raise SocialError("note_not_saved", "原文未保存，关系评分也未建立")
    original_record_id = int(receipt["record_id"])
    evidence_dates = sorted({_utc_day(row["at"]) for row in evidence_rows if _utc_day(row["at"])})
    evidence_times = sorted({float(row["at"]) for row in evidence_rows
                             if isinstance(row["at"], (int, float)) and row["at"] > 0})
    # Durable social observations never expire by calendar.  Old 14-day values
    # remain historical columns but the projection ignores them; staleness is
    # evidence contradiction/supersession/missing text only.
    expires_at = 0.0
    with index.db() as db:
        ensure_schema(db)
        # Re-check under the write lock.  The earlier read only avoided useless
        # work; two concurrent retries must still resolve to one projection.
        db.execute("BEGIN IMMEDIATE")
        row = _existing(db, request_id)
        if row:
            return _duplicate_receipt(db, row)
        latest = _latest_active(db, fields)
        prior_active = _find_prior_impression(db, fields)
        if not latest and mode == "append":
            if prior_active is not None and str(prior_active["session"]) == str(session):
                latest = prior_active
        is_group = str(session).startswith("group:")
        in_scope = (prior_active is not None and (
            (is_group and str(prior_active["session"]).startswith("group:"))
            or (not is_group and str(prior_active["session"]) == str(session))
        ))
        if latest:
            version = int(latest["version"]) + 1
            supersedes_id = int(latest["id"])
        elif prior_active and in_scope:
            version = int(prior_active["version"]) + 1
            supersedes_id = int(prior_active["id"])
        else:
            version = 1
            supersedes_id = None
        fields.update(requested_scores)
        fields["evidence"] = list(requested_evidence)
        inherited, sources = {}, {}
        if _inheritance_eligible(fields):
            sources = _find_manual_sources(db, fields, now)
            for field in ("affinity", "closeness", "confidence"):
                if fields[field] is None and field in sources:
                    fields[field] = int(sources[field]["row"][field])
                    inherited[field] = sources[field]
            if inherited:
                fields["evidence"] = _merge_inherited_evidence(fields["evidence"], inherited, sources)
        evidence_rows = _load_visible_evidence(db, index, session, fields["evidence"], cross)
        if len(evidence_rows) != len(fields["evidence"]):
            raise SocialError("evidence_unreadable", "评分提交前证据已不可读")
        evidence_dates = sorted({_utc_day(r["at"]) for r in evidence_rows if _utc_day(r["at"])})
        evidence_times = sorted({float(r["at"]) for r in evidence_rows if r["at"] > 0})
        if curve_enabled:
            from hakimi.social.affinity_policy import apply_manual_curve
            if requested_scores["affinity"] is None:
                if "affinity" in inherited:
                    source = inherited["affinity"]
                    affinity_policy = apply_manual_curve(
                        db, {**fields, "affinity": None}, impact, now,
                        previous=int(source["row"]["affinity"]),
                        previous_ids=source["evidence_ids"],
                        previous_created_at=source["created_at"],
                        previous_assessment_id=source["source_assessment_id"],
                    )
                else:
                    affinity_policy = apply_manual_curve(
                        db, fields, impact, now, previous=None,
                        previous_ids=[], previous_created_at=None,
                        previous_assessment_id=None,
                    )
            else:
                source = sources.get("affinity")
                affinity_policy = apply_manual_curve(
                    db, fields, impact, now,
                    previous=(int(source["row"]["affinity"]) if source else None),
                    previous_ids=(source["evidence_ids"] if source else []),
                    previous_created_at=(source["created_at"] if source else None),
                    previous_assessment_id=(source["source_assessment_id"]
                                            if source else None),
                )
            fields['affinity'] = affinity_policy['applied']
            if fields.get('closeness') is not None and 'closeness' in sources:
                prev_c = int(sources['closeness']['row']['closeness'])
                close_cap = 2 if impact == 'minor' else 6 if impact == 'ordinary' else 15
                if affinity_policy.get('applied') == affinity_policy.get('previous') and '没有新的互动证据' in affinity_policy.get('policy_reason', ''):
                    fields['closeness'] = prev_c
                else:
                    delta_c = max(-close_cap, min(close_cap, fields['closeness'] - prev_c))
                    fields['closeness'] = max(0, min(100, prev_c + delta_c))
        if inherited:
            affinity_policy = _with_inheritance_policy(
                affinity_policy, fields, inherited, requested_scores, impact)
        if fields["provenance"] == TERRA_PROVENANCE:
            # Terra is an observation, not Hakimi's own curve.  Its scores are a
            # bounded evidence-weighted projection; the exact note is untouched.
            from hakimi.social.affinity_policy import apply_terra_curve
            terra_policy = apply_terra_curve(db, fields, now, evidence_rows)
            for field in ("affinity", "closeness", "confidence"):
                fields[field] = terra_policy["scores"][field]["applied"]
        if latest and _same_revision(latest, fields):
            return _duplicate_receipt(db, latest)
        try:
            cursor = db.execute(
                "INSERT INTO social_assessments(request_id,subject_id,target_id,author,provider,provenance,"
                "impression,reason,affinity,closeness,confidence,relationship_type,evidence_record_ids,"
                "evidence_dates,evidence_times,expires_at,original_record_id,created_at,session,version,"
                "supersedes_id,superseded_by,state,seed_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, fields["subject_id"], fields["target_id"], fields["author"],
                 fields["provider"], fields["provenance"], fields["note"], fields["reason"],
                 fields["affinity"], fields["closeness"], fields["confidence"],
                 fields["relationship_type"], json.dumps(fields["evidence"]),
                 json.dumps(evidence_dates, ensure_ascii=False),
                 json.dumps(evidence_times), expires_at, original_record_id, now,
                 session, version, supersedes_id, None, "active", seed_id),
            )
        except sqlite3.IntegrityError:
            # A concurrent identical request won the unique index race.
            db.rollback()
            row = _existing(db, request_id)
            if row:
                return _duplicate_receipt(db, row)
            raise
        assessment_id = cursor.lastrowid
        if affinity_policy:
            db.execute('UPDATE social_assessments SET affinity_policy=? WHERE id=?',
                       (json.dumps(affinity_policy, ensure_ascii=False), assessment_id))
        if terra_policy:
            db.execute('UPDATE social_assessments SET affinity_policy=? WHERE id=?',
                       (json.dumps(terra_policy, ensure_ascii=False), assessment_id))
        if latest:
            db.execute("UPDATE social_assessments SET state='superseded',superseded_by=? WHERE id=?",
                       (assessment_id, latest["id"]))
        is_hakimi = (fields["subject_id"] == HAKIMI_SUBJECT and fields["provenance"] in ("gemini_manual", "dsh_assisted"))
        if is_hakimi and (mode == "append" or is_group):
            scope_sql = "session LIKE 'group:%'" if is_group else "session=?"
            scope_params = [] if is_group else [str(session)]
            db.execute(
                f"UPDATE social_assessments SET state='superseded', superseded_by=? "
                f"WHERE subject_id=? AND target_id=? AND provenance=? AND state='active' "
                f"AND id!=? AND {scope_sql}",
                [assessment_id, fields["subject_id"], fields["target_id"], fields["provenance"],
                 assessment_id, *scope_params]
            )
            db.execute(
                f"UPDATE records SET state='superseded', superseded_by=? "
                f"WHERE kind='person' AND user_id=? AND key LIKE '关系印象·%' AND state='active' "
                f"AND id!=? AND {scope_sql}",
                [original_record_id, fields["target_id"], original_record_id, *scope_params]
            )
    return {
        "saved": True,
        "created": True,
        "duplicate": False,
        "memory_id": receipt["id"],
        "record_id": original_record_id,
        "hash": receipt["hash"],
        "original_record_id": original_record_id,
        "assessment_id": assessment_id,
        "affinity": fields["affinity"],
        "closeness": fields["closeness"],
        "confidence": fields["confidence"],
        "affinity_policy": terra_policy or affinity_policy,
        "terra_policy": terra_policy,
        "scoring_warning": scoring_warning,
        "version": version,
        "relationship_type": fields["relationship_type"],
        "direction": fields["subject_id"] + "->" + fields["target_id"],
        "provenance": fields["provenance"],
        "mode": mode,
        "index_status": receipt.get("index_status", "pending_fts"),
        "evidence_memory_ids": ["M" + str(value) for value in fields["evidence"]],
    }


def append_social(base, session, note, target_id, **kwargs):
    """Append a short new observation onto the existing active character impression."""
    kwargs["mode"] = "append"
    return record_social(base, session, note, target_id, **kwargs)


def _duplicate_receipt(db, row):
    original = None
    if row["original_record_id"]:
        original = db.execute("SELECT * FROM memory_originals WHERE record_id=? ORDER BY id DESC LIMIT 1",
                              (row["original_record_id"],)).fetchone()
        record = db.execute("SELECT * FROM records WHERE id=?", (row["original_record_id"],)).fetchone()
    else:
        record = None
    return {
        "saved": True,
        "created": False,
        "duplicate": True,
        "memory_id": "M" + str(row["original_record_id"]) if row["original_record_id"] else None,
        "record_id": row["original_record_id"],
        "hash": (original["content_hash"] if original else (record["content_hash"] if record else None)),
        "original_record_id": row["original_record_id"],
        "assessment_id": int(row["id"]),
        "affinity": row["affinity"],
        "closeness": row["closeness"],
        "confidence": row["confidence"],
        "affinity_policy": _stored_affinity_policy(row),
        "version": int(row["version"]),
        "relationship_type": row["relationship_type"],
        "direction": row["subject_id"] + "->" + row["target_id"],
        "provenance": row["provenance"],
        "index_status": "existing",
        "evidence_memory_ids": ["M" + str(value) for value in json.loads(row["evidence_record_ids"] or "[]")],
    }
